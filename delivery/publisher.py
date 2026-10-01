"""Build immutable, checksummed local delivery packages from governed records."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.logging import get_logger, log_event
from storage.state_store import StateStore


LOGGER = get_logger(__name__)


class DeliveryValidationError(RuntimeError):
    """Raised when a batch is not safe to publish."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parse_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else {}


_CHECKSUM_LINE = re.compile(r"^([0-9a-fA-F]{64})  ([^\\/\r\n]+)$")


def _safe_path_component(value: str, label: str) -> str:
    text = str(value).strip()
    candidate = Path(text)
    if not text or text in {".", ".."} or candidate.is_absolute() or candidate.name != text:
        raise ValueError(f"{label} must be a single path component")
    return text


def _write_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(dict(record), ensure_ascii=False, sort_keys=True))
            handle.write("\n")
            count += 1
        handle.flush()
        os.fsync(handle.fileno())
    return count


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(dict(value), handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


class LocalDeliveryPublisher:
    """Validate governed content and publish it as an immutable local release."""

    format_version = "financial-document-delivery-v6"

    def __init__(self, data_root: Path | str, state_store: StateStore | None = None) -> None:
        self.data_root = Path(data_root)
        self.state_store = state_store

    def validate(self, batch_id: str) -> dict[str, Any]:
        """Return publish metrics or raise with a clear, actionable validation error."""

        log_event(LOGGER, "INFO", "release_validation_started", batch_id=batch_id)
        with self._store() as store:
            documents = self._documents(store, batch_id)
            if not documents:
                raise DeliveryValidationError(
                    f"batch {batch_id!r} has no governed documents to publish"
                )

            incomplete = [
                row["governed_uid"] for row in documents if row["status"] != "success"
            ]
            if incomplete:
                raise DeliveryValidationError(
                    "governed documents are not successful: " + ", ".join(incomplete)
                )

            chunks = self._chunks(store, batch_id)
            chunk_counts: dict[str, int] = {}
            invalid_chunks: list[str] = []
            for chunk in chunks:
                chunk_counts[chunk["governed_uid"]] = (
                    chunk_counts.get(chunk["governed_uid"], 0) + 1
                )
                if chunk["status"] != "success" or not _parse_json(chunk["metadata_json"]).get("text"):
                    invalid_chunks.append(chunk["chunk_uid"])
            missing_chunks = [
                row["governed_uid"]
                for row in documents
                if not chunk_counts.get(row["governed_uid"])
            ]
            if missing_chunks:
                raise DeliveryValidationError(
                    "governed documents have no chunks: " + ", ".join(missing_chunks)
                )
            if invalid_chunks:
                raise DeliveryValidationError(
                    "invalid chunks: " + ", ".join(invalid_chunks)
                )

            missing_outputs = [
                row["governed_uid"]
                for row in documents
                if not (
                    self.data_root / Path(row["output_path"]).parent / ".complete"
                ).is_file()
            ]
            if missing_outputs:
                raise DeliveryValidationError(
                    "governed output markers are missing: " + ", ".join(missing_outputs)
                )

            failed_qc = store.connection.execute(
                """
                SELECT DISTINCT qc.entity_uid
                FROM qc_result AS qc
                JOIN governed_document AS gd ON gd.governed_uid = qc.entity_uid
                LEFT JOIN financial_report_version AS frv ON frv.asset_uid = gd.asset_uid
                WHERE gd.batch_id = ?
                  AND (frv.asset_uid IS NULL OR frv.version_status = 'active')
                  AND qc.qc_stage = 'governed' AND qc.status = 'fail'
                """,
                (batch_id,),
            ).fetchall()
            if failed_qc:
                raise DeliveryValidationError(
                    "governed QC failed for: "
                    + ", ".join(str(row["entity_uid"]) for row in failed_qc)
                )

            result = {
                "status": "success",
                "batch_id": batch_id,
                "document_count": len(documents),
                "chunk_count": len(chunks),
                "validated_at": _utc_now(),
            }
            log_event(LOGGER, "INFO", "release_validation_finished", **result)
            return result

    def publish(
        self,
        batch_id: str,
        release_id: str,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Publish one immutable local package and atomically update ``latest.json``."""

        log_event(
            LOGGER,
            "INFO",
            "release_publish_started",
            batch_id=batch_id,
            release_id=release_id,
            dry_run=dry_run,
        )
        validation = self.validate(batch_id)
        release_path = self._release_path(batch_id, release_id)
        if dry_run:
            result = {
                **validation,
                "release_id": release_id,
                "release_path": str(release_path),
                "dry_run": True,
            }
            log_event(LOGGER, "INFO", "release_publish_finished", **result)
            return result
        if release_path.exists():
            raise FileExistsError(f"delivery release already exists: {release_path}")

        release_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path = Path(
            tempfile.mkdtemp(prefix=f".{release_id}.", dir=release_path.parent)
        )
        try:
            with self._store() as store:
                financial_metadata_records = list(
                    self._financial_metadata_records(store, batch_id)
                )
                ocr_quality_records = list(self._ocr_quality_records(store, batch_id))
                metadata_override_audit_records = store.list_financial_metadata_override_audits(
                    [record["asset_uid"] for record in financial_metadata_records]
                )
                report_version_records = list(self._report_version_records(store, batch_id))
                report_version_audit_records = store.list_financial_report_version_selection_audits(
                    [record["report_group_uid"] for record in report_version_records]
                )
                metadata_count = _write_jsonl(
                    staging_path / "metadata.jsonl",
                    self._metadata_records(store, batch_id),
                )
                governed_document_count = _write_jsonl(
                    staging_path / "governed-documents.jsonl",
                    self._governed_document_records(store, batch_id),
                )
                financial_metadata_count = _write_jsonl(
                    staging_path / "financial_metadata.jsonl", financial_metadata_records
                )
                ocr_quality_count = _write_jsonl(
                    staging_path / "ocr-quality.jsonl", ocr_quality_records
                )
                metadata_override_audit_count = _write_jsonl(
                    staging_path / "metadata-override-audit.jsonl",
                    metadata_override_audit_records,
                )
                report_version_count = _write_jsonl(
                    staging_path / "financial-report-versions.jsonl",
                    report_version_records,
                )
                report_version_audit_count = _write_jsonl(
                    staging_path / "version-selection-audit.jsonl",
                    report_version_audit_records,
                )
                chunk_count = _write_jsonl(
                    staging_path / "chunks.jsonl",
                    self._chunk_records(store, batch_id),
                )
                artifact_count = _write_jsonl(
                    staging_path / "artifacts.jsonl",
                    self._artifact_records(store, batch_id),
                )
            metadata_qc_summary = self._metadata_qc_summary(financial_metadata_records)
            _write_json(staging_path / "metadata-qc-summary.json", metadata_qc_summary)
            ocr_quality_summary = self._ocr_quality_summary(ocr_quality_records)
            _write_json(staging_path / "ocr-quality-summary.json", ocr_quality_summary)

            manifest = {
                "format_version": self.format_version,
                "batch_id": batch_id,
                "release_id": release_id,
                "published_at": _utc_now(),
                "document_count": metadata_count,
                "governed_document_count": governed_document_count,
                "financial_metadata_count": financial_metadata_count,
                "ocr_quality_count": ocr_quality_count,
                "metadata_override_audit_count": metadata_override_audit_count,
                "financial_report_version_count": report_version_count,
                "version_selection_audit_count": report_version_audit_count,
                "chunk_count": chunk_count,
                "artifact_count": artifact_count,
                "metadata_qc": metadata_qc_summary,
                "ocr_quality": ocr_quality_summary,
                "validation": validation,
            }
            _write_json(staging_path / "manifest.json", manifest)
            checksum_paths = [
                staging_path / "metadata.jsonl",
                staging_path / "governed-documents.jsonl",
                staging_path / "financial_metadata.jsonl",
                staging_path / "ocr-quality.jsonl",
                staging_path / "metadata-override-audit.jsonl",
                staging_path / "financial-report-versions.jsonl",
                staging_path / "version-selection-audit.jsonl",
                staging_path / "chunks.jsonl",
                staging_path / "artifacts.jsonl",
                staging_path / "metadata-qc-summary.json",
                staging_path / "ocr-quality-summary.json",
                staging_path / "manifest.json",
            ]
            with (staging_path / "checksums.sha256").open("w", encoding="ascii") as handle:
                for path in checksum_paths:
                    handle.write(f"{_sha256_file(path)}  {path.name}\n")
                handle.flush()
                os.fsync(handle.fileno())

            os.replace(staging_path, release_path)
            self._write_latest_pointer(batch_id, release_id, release_path)
            result = {
                **validation,
                "release_id": release_id,
                "release_path": str(release_path),
                "manifest_path": str(release_path / "manifest.json"),
                "dry_run": False,
            }
            log_event(LOGGER, "INFO", "release_publish_finished", **result)
            return result
        except Exception as exc:
            log_event(
                LOGGER,
                "ERROR",
                "release_publish_failed",
                batch_id=batch_id,
                release_id=release_id,
                error_msg=f"{type(exc).__name__}: {exc}",
            )
            if staging_path.exists():
                shutil.rmtree(staging_path)
            raise

    def verify(self, batch_id: str, release_id: str) -> dict[str, Any]:
        """Verify every release file against its immutable SHA-256 manifest."""

        log_event(
            LOGGER,
            "INFO",
            "release_verification_started",
            batch_id=batch_id,
            release_id=release_id,
        )
        release_path = self._release_path(batch_id, release_id)
        checksum_path = release_path / "checksums.sha256"
        manifest_file = release_path / "manifest.json"
        if manifest_file.exists():
            try:
                manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                manifest = {}
            if isinstance(manifest, dict) and manifest.get("format_version") == "financial-document-delivery-v7":
                from training.flywheel_corpus import verify
                try:
                    verify(release_path)
                    return {"status": "success", "errors": [], "release_path": str(release_path)}
                except (ValueError, OSError) as exc:
                    return {"status": "failed", "errors": [{"error": str(exc)}], "release_path": str(release_path)}
        errors: list[dict[str, str]] = []
        expected: dict[str, str] = {}
        if not checksum_path.is_file():
            errors.append(
                {"path": "checksums.sha256", "error": "checksum manifest is missing"}
            )
        else:
            try:
                lines = checksum_path.read_text(encoding="ascii").splitlines()
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(
                    {
                        "path": "checksums.sha256",
                        "error": f"cannot read checksum manifest: {type(exc).__name__}: {exc}",
                    }
                )
                lines = []
            for line_number, line in enumerate(lines, start=1):
                match = _CHECKSUM_LINE.fullmatch(line)
                if match is None:
                    errors.append(
                        {
                            "path": "checksums.sha256",
                            "error": f"invalid checksum entry at line {line_number}",
                        }
                    )
                    continue
                expected_hash, file_name = match.groups()
                if file_name in {".", ".."} or ":" in file_name:
                    errors.append(
                        {
                            "path": "checksums.sha256",
                            "error": f"unsafe checksum filename at line {line_number}",
                        }
                    )
                    continue
                if file_name in expected:
                    errors.append(
                        {
                            "path": "checksums.sha256",
                            "error": f"duplicate checksum entry for {file_name}",
                        }
                    )
                    continue
                expected[file_name] = expected_hash.lower()
        if checksum_path.is_file() and not expected and not errors:
            errors.append({"path": "checksums.sha256", "error": "checksum manifest is empty"})

        for file_name, expected_hash in expected.items():
            path = release_path / file_name
            if not path.is_file():
                errors.append({"path": file_name, "error": "release file is missing"})
                continue
            actual_hash = _sha256_file(path)
            if actual_hash != expected_hash:
                errors.append(
                    {
                        "path": file_name,
                        "error": "SHA-256 does not match",
                        "expected_sha256": expected_hash,
                        "actual_sha256": actual_hash,
                    }
                )
            else:
                log_event(
                    LOGGER,
                    "DEBUG",
                    "release_file_verified",
                    batch_id=batch_id,
                    release_id=release_id,
                    file_name=file_name,
                )
        result = {
            "status": "success" if not errors else "failed",
            "batch_id": batch_id,
            "release_id": release_id,
            "release_path": str(release_path),
            "verified_file_count": len(expected),
            "errors": errors,
            "verified_at": _utc_now(),
        }
        log_event(
            LOGGER,
            "INFO" if not errors else "ERROR",
            "release_verification_finished",
            batch_id=batch_id,
            release_id=release_id,
            status=result["status"],
            verified_file_count=len(expected),
            error_count=len(errors),
        )
        return result

    def _store(self):
        if self.state_store is not None:
            return _StoreContext(self.state_store, close_when_done=False)
        store = StateStore(self.data_root / "state" / "pipeline.db")
        return _StoreContext(store, close_when_done=True)

    def _documents(self, store: StateStore, batch_id: str):
        return store.connection.execute(
            """
            SELECT gd.*
            FROM governed_document AS gd
            LEFT JOIN financial_report_version AS frv ON frv.asset_uid = gd.asset_uid
            WHERE gd.batch_id = ?
              AND (frv.asset_uid IS NULL OR frv.version_status = 'active')
            ORDER BY gd.document_uid, gd.governed_uid
            """,
            (batch_id,),
        ).fetchall()

    def _chunks(self, store: StateStore, batch_id: str):
        return store.connection.execute(
            """
            SELECT dc.*
            FROM document_chunk AS dc
            JOIN governed_document AS gd ON gd.governed_uid = dc.governed_uid
            LEFT JOIN financial_report_version AS frv ON frv.asset_uid = gd.asset_uid
            WHERE gd.batch_id = ?
              AND (frv.asset_uid IS NULL OR frv.version_status = 'active')
            ORDER BY dc.document_uid, dc.chunk_index
            """,
            (batch_id,),
        ).fetchall()

    def _metadata_records(self, store: StateStore, batch_id: str):
        for row in self._documents(store, batch_id):
            record = dict(row)
            record["metadata"] = _parse_json(record.pop("metadata_json"))
            yield record

    def _governed_document_records(self, store: StateStore, batch_id: str):
        """Materialize the complete deterministic training source for a release."""

        for row in self._documents(store, batch_id):
            record = dict(row)
            metadata = _parse_json(record["metadata_json"])
            text = self._read_governed_text(record)
            yield {
                "schema_version": "governed-document-v1",
                "governed_uid": record["governed_uid"],
                "document_uid": record["document_uid"],
                "work_uid": record["work_uid"],
                "asset_uid": record["asset_uid"],
                "batch_id": record["batch_id"],
                "backend": record["backend"],
                "rule_version": record["rule_version"],
                "document_type": metadata.get("document_type"),
                "title": metadata.get("title"),
                "source_name": metadata.get("source_name"),
                "source_id": metadata.get("source_id"),
                "text": text,
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "char_count": len(text),
            }

    def _read_governed_text(self, record: Mapping[str, Any]) -> str:
        output_value = str(record.get("output_path") or "").strip()
        if not output_value:
            raise DeliveryValidationError(
                f"governed document {record.get('governed_uid')!r} has no output path"
            )
        output_path = (self.data_root / output_value).resolve()
        data_root = self.data_root.resolve()
        if output_path != data_root and data_root not in output_path.parents:
            raise DeliveryValidationError(
                f"governed document output escapes data root: {output_value}"
            )
        try:
            text = output_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise DeliveryValidationError(
                f"cannot read governed document {record.get('governed_uid')!r}: {exc}"
            ) from exc
        if not text.strip():
            raise DeliveryValidationError(
                f"governed document {record.get('governed_uid')!r} has empty text"
            )
        return text

    def _financial_metadata_records(self, store: StateStore, batch_id: str):
        rows = store.connection.execute(
            """
            SELECT DISTINCT fdm.*
            FROM financial_document_metadata AS fdm
            JOIN governed_document AS gd ON gd.asset_uid = fdm.asset_uid
            LEFT JOIN financial_report_version AS frv ON frv.asset_uid = gd.asset_uid
            WHERE gd.batch_id = ?
              AND (frv.asset_uid IS NULL OR frv.version_status = 'active')
            ORDER BY fdm.document_uid, fdm.asset_uid
            """,
            (batch_id,),
        ).fetchall()
        for row in rows:
            record = _parse_json(row["metadata_json"])
            record.setdefault("asset_uid", row["asset_uid"])
            record.setdefault("document_uid", row["document_uid"])
            record.setdefault("metadata_hash", row["metadata_hash"])
            record.setdefault("status", row["status"])
            yield record

    def _ocr_quality_records(self, store: StateStore, batch_id: str):
        rows = store.connection.execute(
            """
            SELECT DISTINCT oq.*
            FROM ocr_quality AS oq
            JOIN governed_document AS gd ON gd.asset_uid = oq.asset_uid
            LEFT JOIN financial_report_version AS frv ON frv.asset_uid = gd.asset_uid
            WHERE gd.batch_id = ?
              AND (frv.asset_uid IS NULL OR frv.version_status = 'active')
            ORDER BY oq.document_uid, oq.asset_uid
            """,
            (batch_id,),
        ).fetchall()
        for row in rows:
            record = dict(row)
            record["metrics"] = _parse_json(record.pop("metrics_json"))
            checks_value = record.pop("checks_json")
            try:
                checks = json.loads(checks_value) if checks_value else []
            except json.JSONDecodeError:
                checks = []
            record["qc_checks"] = checks if isinstance(checks, list) else []
            yield record

    def _report_version_records(self, store: StateStore, batch_id: str):
        rows = store.connection.execute(
            """
            SELECT DISTINCT frv.*
            FROM financial_report_version AS frv
            JOIN governed_document AS gd ON gd.asset_uid = frv.asset_uid
            WHERE gd.batch_id = ?
            ORDER BY frv.report_group_uid, frv.created_at, frv.asset_uid
            """,
            (batch_id,),
        ).fetchall()
        for row in rows:
            yield dict(row)

    def _chunk_records(self, store: StateStore, batch_id: str):
        for row in self._chunks(store, batch_id):
            record = dict(row)
            record["metadata"] = _parse_json(record.pop("metadata_json"))
            yield record

    def _artifact_records(self, store: StateStore, batch_id: str):
        rows = store.connection.execute(
            """
            SELECT DISTINCT artifact.*
            FROM artifact
            JOIN governed_document AS gd ON gd.asset_uid = artifact.asset_uid
            LEFT JOIN financial_report_version AS frv ON frv.asset_uid = gd.asset_uid
            WHERE gd.batch_id = ?
              AND (frv.asset_uid IS NULL OR frv.version_status = 'active')
            ORDER BY artifact.artifact_type, artifact.path
            """,
            (batch_id,),
        ).fetchall()
        for row in rows:
            record = dict(row)
            record["metadata"] = _parse_json(record.pop("metadata_json"))
            yield record

    @staticmethod
    def _metadata_qc_summary(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        """Summarize metadata checks as delivery context, never as a release gate."""

        status_counts: Counter[str] = Counter()
        check_counts: Counter[str] = Counter()
        documents_needing_review: list[dict[str, Any]] = []
        record_count = 0
        for record in records:
            record_count += 1
            checks = record.get("qc_checks") or []
            for check in checks:
                if not isinstance(check, Mapping):
                    continue
                status = str(check.get("status") or "unknown")
                status_counts[status] += 1
                if status == "warn":
                    check_counts[str(check.get("check_name") or "unknown")] += 1
            missing_fields = record.get("missing_fields") or []
            if missing_fields or record.get("status") == "needs_review":
                documents_needing_review.append(
                    {
                        "asset_uid": record.get("asset_uid"),
                        "document_uid": record.get("document_uid"),
                        "missing_fields": missing_fields,
                    }
                )
        return {
            "blocking": False,
            "metadata_record_count": record_count,
            "check_status_counts": dict(sorted(status_counts.items())),
            "warning_counts_by_check": dict(sorted(check_counts.items())),
            "documents_needing_review": documents_needing_review,
        }

    @staticmethod
    def _ocr_quality_summary(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        """Summarize OCR quality warnings for delivery context without gating release."""

        status_counts: Counter[str] = Counter()
        warning_counts: Counter[str] = Counter()
        assets_needing_review: list[dict[str, Any]] = []
        record_count = 0
        for record in records:
            record_count += 1
            status_counts[str(record.get("status") or "unknown")] += 1
            warnings = [
                check
                for check in record.get("qc_checks") or []
                if isinstance(check, Mapping) and check.get("status") == "warn"
            ]
            for check in warnings:
                warning_counts[str(check.get("check_name") or "unknown")] += 1
            if warnings:
                assets_needing_review.append(
                    {
                        "asset_uid": record.get("asset_uid"),
                        "document_uid": record.get("document_uid"),
                        "warning_count": len(warnings),
                    }
                )
        return {
            "blocking": False,
            "quality_record_count": record_count,
            "status_counts": dict(sorted(status_counts.items())),
            "warning_counts_by_check": dict(sorted(warning_counts.items())),
            "assets_needing_review": assets_needing_review,
        }

    def _write_latest_pointer(
        self, batch_id: str, release_id: str, release_path: Path
    ) -> None:
        path = release_path.parent / "latest.json"
        fd, temporary_name = tempfile.mkstemp(
            prefix=".latest.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    {
                        "batch_id": batch_id,
                        "release_id": release_id,
                        "release_path": release_id,
                        "published_at": _utc_now(),
                    },
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _release_path(self, batch_id: str, release_id: str) -> Path:
        return (
            self.data_root
            / "published"
            / _safe_path_component(batch_id, "batch_id")
            / _safe_path_component(release_id, "release_id")
        )


class _StoreContext:
    def __init__(self, store: StateStore, *, close_when_done: bool) -> None:
        self.store = store
        self.close_when_done = close_when_done

    def __enter__(self) -> StateStore:
        return self.store

    def __exit__(self, *_args: object) -> None:
        if self.close_when_done:
            self.store.close()
