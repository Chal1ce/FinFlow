"""Import local financial PDFs and materialize OCR outputs under one contract."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from config import load_config as load_runtime_config
from core.context import PipelineContext
from core.ids import artifact_uid, logical_report_uid, raw_asset_uid, stable_uid
from qc.validators import qc_passed, validate_asset
from spiders.downloader import inspect_pdf, sha256_file, utc_now
from spiders.http_client import write_jsonl_atomic
from storage.state_store import StateStore


LOCAL_DOCUMENT_MANIFEST = "local_documents.jsonl"
OcrProcessor = Callable[[Path, Path], Mapping[str, Any]]


def load_local_documents(data_root: Path | str) -> dict[str, dict[str, Any]]:
    """Load the current local-import manifest indexed by immutable asset ID."""

    path = Path(data_root) / "manifests" / LOCAL_DOCUMENT_MANIFEST
    records: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return records
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid local document manifest line {line_number}") from exc
            if not isinstance(record, dict) or not record.get("asset_uid"):
                raise ValueError(
                    f"local document manifest line {line_number} is missing asset_uid"
                )
            records[str(record["asset_uid"])] = record
    return records


def _write_local_documents(data_root: Path, records: Mapping[str, Mapping[str, Any]]) -> None:
    path = data_root / "manifests" / LOCAL_DOCUMENT_MANIFEST
    ordered = sorted(
        (dict(record) for record in records.values()),
        key=lambda record: (str(record.get("document_uid") or ""), str(record["asset_uid"])),
    )
    write_jsonl_atomic(path, ordered)


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_handle:
            shutil.copyfileobj(input_handle, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, target)
    finally:
        temporary_path.unlink(missing_ok=True)


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(value), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


class LocalPdfImporter:
    """Copy user-supplied PDFs into the local data lake with stable lineage."""

    def __init__(
        self,
        data_root: Path | str,
        *,
        state_store: StateStore | None = None,
        min_pdf_size_bytes: int = 100,
    ) -> None:
        self.data_root = Path(data_root)
        self.state_store = state_store
        self.min_pdf_size_bytes = min_pdf_size_bytes

    def run(
        self,
        input_path: Path | str,
        *,
        batch_id: str,
        metadata: Mapping[str, Any] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        source_paths = self._find_pdfs(Path(input_path))
        return self.run_records(
            ((source_path, metadata or {}) for source_path in source_paths),
            batch_id=batch_id,
            dry_run=dry_run,
        )

    def run_records(
        self,
        sources: Iterable[tuple[Path | str, Mapping[str, Any]]],
        *,
        batch_id: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Import multiple PDFs with per-document provenance in one state run."""

        source_items = [(Path(path), dict(metadata)) for path, metadata in sources]
        if not source_items:
            raise ValueError("at least one PDF source is required")
        context = PipelineContext.create(
            self.data_root, batch_id=batch_id, config_version="local-import-v1"
        )
        existing_records = load_local_documents(self.data_root)
        store = self.state_store or StateStore(self.data_root / "state" / "pipeline.db")
        owns_store = self.state_store is None
        records: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        counts = {"imported": 0, "skipped": 0, "preview": 0, "failed": 0}
        try:
            store.start_run(context, dry_run=dry_run)
            for source_path, metadata in source_items:
                record, error = self._prepare_record(source_path, batch_id, metadata)
                entity_uid = str(record.get("asset_uid") or source_path.name)
                step_id = store.start_step(
                    context,
                    "local-import",
                    entity_uid,
                    metadata={"source_path": str(source_path)},
                )
                if error:
                    counts["failed"] += 1
                    errors.append({"source_path": str(source_path), "error_msg": error})
                    record.update({"status": "failed", "error_msg": error})
                    records.append(record)
                    store.finish_step(step_id, "failed", error)
                    continue
                try:
                    target = self.data_root / str(record["raw_path"])
                    if dry_run:
                        disposition = "preview"
                    elif target.exists():
                        if sha256_file(target) != record["raw_file_hash"]:
                            raise RuntimeError(f"existing asset hash does not match: {target}")
                        disposition = "skipped"
                    else:
                        _atomic_copy(source_path, target)
                        disposition = "imported"

                    record.update(
                        {
                            "status": "success" if not dry_run else "preview",
                            "import_disposition": disposition,
                            "imported_at": utc_now(),
                        }
                    )
                    counts[disposition] += 1
                    if not dry_run:
                        checks = validate_asset(
                            record, self.data_root, min_size_bytes=self.min_pdf_size_bytes
                        )
                        store.upsert_document(
                            context,
                            {
                                "document_uid": record["document_uid"],
                                "document_type": record["document_type"],
                                "title": record["title"],
                                "source_name": record["source_name"],
                                "source_id": record["source_id"],
                                "metadata": record,
                            },
                        )
                        store.upsert_asset(context, record)
                        store.record_qc(
                            context,
                            record["asset_uid"],
                            "raw_asset",
                            [check.as_mapping() for check in checks],
                        )
                        if not qc_passed(checks):
                            raise RuntimeError("imported PDF did not pass raw asset QC")
                        existing = existing_records.get(record["asset_uid"], {})
                        batch_ids = set(existing.get("batch_ids") or ())
                        if existing.get("batch_id"):
                            batch_ids.add(str(existing["batch_id"]))
                        batch_ids.add(batch_id)
                        record["batch_ids"] = sorted(batch_ids)
                        existing_records[record["asset_uid"]] = dict(record)
                    records.append(record)
                    store.finish_step(step_id, "success")
                except (OSError, RuntimeError, ValueError) as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    counts["failed"] += 1
                    errors.append({"source_path": str(source_path), "error_msg": message})
                    record.update({"status": "failed", "error_msg": message})
                    records.append(record)
                    store.finish_step(step_id, "failed", message)
            if not dry_run:
                _write_local_documents(self.data_root, existing_records)
            status = "failed" if errors else "success"
            store.finish_run(context.run_id, status, "one or more PDFs failed to import" if errors else None)
            return {
                "status": status,
                "batch_id": batch_id,
                "run_id": context.run_id,
                "dry_run": dry_run,
                "counts": counts,
                "records": records,
                "errors": errors,
            }
        except Exception as exc:
            store.finish_run(context.run_id, "failed", f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if owns_store:
                store.close()

    def _find_pdfs(self, input_path: Path) -> list[Path]:
        if not input_path.exists():
            raise FileNotFoundError(input_path)
        if input_path.is_file():
            if input_path.suffix.lower() != ".pdf":
                raise ValueError(f"input is not a PDF: {input_path}")
            return [input_path]
        paths = sorted(path for path in input_path.rglob("*.pdf") if path.is_file())
        if not paths:
            raise ValueError(f"no PDF files found under {input_path}")
        return paths

    def _prepare_record(
        self, source_path: Path, batch_id: str, metadata: Mapping[str, Any]
    ) -> tuple[dict[str, Any], str | None]:
        inspection = inspect_pdf(source_path, min_size_bytes=self.min_pdf_size_bytes)
        raw_file_hash = sha256_file(source_path)
        asset_uid = raw_asset_uid(raw_file_hash)
        document_uid = self._document_uid(source_path, raw_file_hash, metadata)
        manifest_hash = str(metadata.get("raw_file_hash") or "").strip()
        source_name = str(metadata.get("source_name") or "local-import")
        source_id = str(metadata.get("source_id") or source_path.name)
        raw_path = str(
            metadata.get("raw_path")
            or (Path("raw_pdfs") / "imported" / f"{asset_uid}.pdf").as_posix()
        )
        source_metadata = metadata.get("source_metadata")
        record = {
            "schema_version": "local-document-v1",
            "import_metadata": dict(metadata),
            "batch_id": batch_id,
            "document_uid": document_uid,
            "work_uid": str(metadata.get("work_uid") or document_uid),
            "report_uid": str(metadata.get("report_uid") or document_uid),
            "asset_uid": asset_uid,
            "document_type": str(metadata.get("document_type") or "financial-report"),
            "title": str(metadata.get("title") or source_path.stem),
            "source_name": source_name,
            "source_id": source_id,
            "source_url": str(metadata.get("source_url") or source_path.resolve().as_uri()),
            "source_path": str(metadata.get("source_path") or source_path.resolve()),
            "source_metadata": dict(source_metadata)
            if isinstance(source_metadata, Mapping)
            else {},
            "raw_path": raw_path,
            "raw_file_hash": raw_file_hash,
            "file_size": inspection.file_size,
            "page_count": inspection.page_count,
            "stock_code": metadata.get("stock_code"),
            "company_name": metadata.get("company_name"),
            "report_year": metadata.get("report_year"),
            "report_period": metadata.get("report_period"),
            "report_type": metadata.get("report_type"),
            "announcement_date": metadata.get("announcement_date"),
            "language": metadata.get("language"),
            "document_variant": metadata.get("document_variant"),
        }
        if manifest_hash and manifest_hash != raw_file_hash:
            return record, "source manifest hash does not match the PDF content"
        return record, inspection.error

    @staticmethod
    def _document_uid(
        source_path: Path, raw_file_hash: str, metadata: Mapping[str, Any]
    ) -> str:
        explicit_uid = str(metadata.get("document_uid") or "").strip()
        if explicit_uid:
            return explicit_uid
        stock_code = str(metadata.get("stock_code") or "").strip()
        report_year = metadata.get("report_year")
        report_type = str(metadata.get("report_type") or "").strip()
        if stock_code and report_year and report_type:
            try:
                return logical_report_uid(
                    stock_code.zfill(6),
                    int(report_year),
                    report_type,
                    language=str(metadata.get("language") or "zh-CN"),
                    document_variant=str(metadata.get("document_variant") or "full"),
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("report_year must be an integer") from exc
        return stable_uid("local-document", source_path.stem.lower(), raw_file_hash)


class CollectedReportAdopter:
    """Adopt successfully collected financial reports into the local OCR contract."""

    manifest_name = "reports.jsonl"
    eligible_statuses = {"success", "duplicate", "skipped"}

    def __init__(
        self,
        data_root: Path | str,
        *,
        state_store: StateStore | None = None,
        min_pdf_size_bytes: int = 100,
    ) -> None:
        self.data_root = Path(data_root)
        self.state_store = state_store
        self.min_pdf_size_bytes = min_pdf_size_bytes

    def run(self, *, batch_id: str, dry_run: bool = False) -> dict[str, Any]:
        """Materialize the collected assets for one batch without losing provenance."""

        candidates = self._load_candidates(batch_id)
        if not candidates:
            return {
                "status": "skipped",
                "batch_id": batch_id,
                "dry_run": dry_run,
                "reason": "no successful collected financial reports are associated with this batch",
                "counts": {"imported": 0, "skipped": 0, "preview": 0, "failed": 0},
                "records": [],
                "errors": [],
            }

        sources: list[tuple[Path, Mapping[str, Any]]] = []
        errors: list[dict[str, str]] = []
        for report in candidates:
            raw_path = str(report.get("raw_path") or "")
            try:
                source_path = self._source_path(raw_path)
                if not source_path.is_file():
                    raise FileNotFoundError(source_path)
                if source_path.suffix.lower() != ".pdf":
                    raise ValueError(f"collected path is not a PDF: {raw_path}")
            except (OSError, ValueError) as exc:
                errors.append(
                    {
                        "source_path": raw_path,
                        "error_msg": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            sources.append((source_path, self._import_metadata(report, raw_path)))

        if not sources:
            return {
                "status": "failed",
                "batch_id": batch_id,
                "dry_run": dry_run,
                "counts": {"imported": 0, "skipped": 0, "preview": 0, "failed": len(errors)},
                "records": [],
                "errors": errors,
            }

        result = LocalPdfImporter(
            self.data_root,
            state_store=self.state_store,
            min_pdf_size_bytes=self.min_pdf_size_bytes,
        ).run_records(sources, batch_id=batch_id, dry_run=dry_run)
        if errors:
            result = {
                **result,
                "status": "failed",
                "errors": [*result["errors"], *errors],
                "counts": {**result["counts"], "failed": result["counts"]["failed"] + len(errors)},
            }
        return {**result, "adopted_from": self.manifest_name}

    def _load_candidates(self, batch_id: str) -> list[dict[str, Any]]:
        path = self.data_root / "manifests" / self.manifest_name
        if not path.exists():
            return []
        records: dict[str, dict[str, Any]] = {}
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid report manifest line {line_number}") from exc
                if not isinstance(record, dict):
                    raise ValueError(f"report manifest line {line_number} is not an object")
                batch_ids = {str(item) for item in record.get("batch_ids") or ()}
                if record.get("batch_id"):
                    batch_ids.add(str(record["batch_id"]))
                if (
                    batch_id in batch_ids
                    and record.get("status") in self.eligible_statuses
                    and record.get("asset_uid")
                    and record.get("raw_path")
                ):
                    records[str(record["asset_uid"])] = record
        return [records[asset_uid] for asset_uid in sorted(records)]

    def _source_path(self, raw_path: str) -> Path:
        relative_path = Path(raw_path)
        if not raw_path or relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"unsafe collected raw_path: {raw_path!r}")
        root = self.data_root.resolve()
        candidate = (root / relative_path).resolve()
        if root not in candidate.parents:
            raise ValueError(f"collected raw_path escapes data root: {raw_path!r}")
        return candidate

    @staticmethod
    def _import_metadata(report: Mapping[str, Any], raw_path: str) -> dict[str, Any]:
        return {
            "document_uid": report.get("document_uid") or report.get("report_uid"),
            "report_uid": report.get("report_uid"),
            "work_uid": report.get("work_uid") or report.get("report_uid"),
            "document_type": "financial-report",
            "title": report.get("title"),
            "source_name": report.get("source_name"),
            "source_id": report.get("source_id"),
            "source_url": report.get("source_url"),
            "source_path": raw_path,
            "source_metadata": {"report_manifest": dict(report)},
            "raw_path": raw_path,
            "raw_file_hash": report.get("raw_file_hash"),
            "stock_code": report.get("stock_code"),
            "company_name": report.get("company_name"),
            "report_year": report.get("report_year"),
            "report_period": report.get("report_period"),
            "report_type": report.get("report_type"),
            "announcement_date": report.get("publish_date"),
            "language": report.get("language"),
            "document_variant": report.get("document_variant"),
        }


class LocalDocumentOcrRunner:
    """Run OCR for imported financial PDFs and preserve input/output lineage."""

    def __init__(
        self,
        data_root: Path | str,
        *,
        processor: OcrProcessor | None = None,
        state_store: StateStore | None = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.processor = processor
        self.state_store = state_store

    def run(
        self,
        *,
        batch_id: str,
        backend: str,
        dry_run: bool = False,
        force: bool = False,
        asset_uids: Iterable[str] | None = None,
        retry_reason: str | None = None,
    ) -> dict[str, Any]:
        if backend not in {"local", "cloud"}:
            raise ValueError("OCR backend must be local or cloud")
        selected_assets = {str(asset_uid) for asset_uid in asset_uids or ()}
        records = [
            record
            for record in load_local_documents(self.data_root).values()
            if (
                batch_id in set(record.get("batch_ids") or (record.get("batch_id"),))
                and record.get("status") == "success"
                and (not selected_assets or str(record["asset_uid"]) in selected_assets)
            )
        ]
        if not records:
            target = "selected assets" if selected_assets else "imported PDFs"
            raise ValueError(f"batch {batch_id!r} has no {target} ready for OCR")
        context = PipelineContext.create(
            self.data_root, batch_id=batch_id, config_version="financial-ocr-v1"
        )
        store = self.state_store or StateStore(self.data_root / "state" / "pipeline.db")
        owns_store = self.state_store is None
        processor = self.processor
        if processor is None and not dry_run:
            processor = self._build_processor(backend)
        all_records = load_local_documents(self.data_root)
        counts = {"processed": 0, "skipped": 0, "preview": 0, "failed": 0}
        errors: list[dict[str, str]] = []
        try:
            store.start_run(context, dry_run=dry_run)
            for record in sorted(records, key=lambda item: str(item["asset_uid"])):
                asset_uid = str(record["asset_uid"])
                step_id = store.start_step(
                    context,
                    "financial-ocr",
                    asset_uid,
                    metadata={"backend": backend, "raw_path": record["raw_path"]},
                )
                output_dir = self._output_dir(record, backend)
                attempt_uid = stable_uid(
                    "financial-ocr-attempt", asset_uid, backend, context.run_id
                )
                archive_path: Path | None = None
                try:
                    source_path = self.data_root / str(record["raw_path"])
                    if not source_path.is_file():
                        raise FileNotFoundError(source_path)
                    if dry_run:
                        disposition = "preview"
                        result: Mapping[str, Any] = {}
                    elif (output_dir / "output.md").is_file() and not force:
                        disposition = "skipped"
                        result = {}
                    else:
                        if force and output_dir.exists():
                            archive_path = self._archive_existing_output(
                                output_dir, backend, attempt_uid
                            )
                            self._clear_materialized_ocr_output(output_dir)
                        _write_json_atomic(
                            output_dir / "input.json",
                            {
                                "batch_id": batch_id,
                                "run_id": context.run_id,
                                "document_uid": record["document_uid"],
                                "asset_uid": asset_uid,
                                "backend": backend,
                                "raw_path": record["raw_path"],
                                "input_hash": record["raw_file_hash"],
                                "written_at": utc_now(),
                            },
                        )
                        if processor is None:
                            raise RuntimeError("OCR processor is unavailable")
                        result = processor(source_path, output_dir)
                        output_text = (output_dir / "output.md").read_text(encoding="utf-8").strip()
                        if not output_text:
                            raise RuntimeError("OCR output.md is empty")
                        disposition = "processed"

                    counts[disposition] += 1
                    attempt = {
                        "attempt_uid": attempt_uid,
                        "run_id": context.run_id,
                        "backend": backend,
                        "retry_reason": retry_reason,
                        "forced": force,
                        "status": "success" if disposition != "preview" else "preview",
                        "archived_output_dir": (
                            archive_path.relative_to(self.data_root).as_posix()
                            if archive_path is not None
                            else None
                        ),
                        "completed_at": utc_now(),
                    }
                    attempts = list(record.get("ocr_attempts") or ())
                    if disposition != "skipped":
                        attempts.append(attempt)
                    updated = {
                        **record,
                        "ocr_status": "success" if disposition != "preview" else "preview",
                        "ocr_backend": backend,
                        "ocr_output_dir": output_dir.relative_to(self.data_root).as_posix(),
                        "ocr_result": dict(result),
                        "ocr_updated_at": utc_now(),
                        "ocr_attempts": attempts,
                    }
                    if not dry_run:
                        if disposition == "processed":
                            _write_json_atomic(output_dir / "attempt.json", attempt)
                        self._record_ocr_artifacts(store, context, updated, output_dir, backend)
                        store.record_qc(
                            context,
                            asset_uid,
                            "ocr",
                            [
                                {
                                    "check_name": "ocr_output_markdown",
                                    "status": "pass",
                                    "value": {"output_dir": updated["ocr_output_dir"]},
                                }
                            ],
                        )
                        all_records[asset_uid] = updated
                    store.finish_step(step_id, "success")
                except (OSError, RuntimeError, ValueError) as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    counts["failed"] += 1
                    errors.append({"asset_uid": asset_uid, "error_msg": message})
                    all_records[asset_uid] = {
                        **record,
                        "ocr_status": "failed",
                        "ocr_backend": backend,
                        "ocr_error": message,
                        "ocr_updated_at": utc_now(),
                        "ocr_attempts": [
                            *list(record.get("ocr_attempts") or ()),
                            {
                                "attempt_uid": attempt_uid,
                                "run_id": context.run_id,
                                "backend": backend,
                                "retry_reason": retry_reason,
                                "forced": force,
                                "status": "failed",
                                "archived_output_dir": (
                                    archive_path.relative_to(self.data_root).as_posix()
                                    if archive_path is not None
                                    else None
                                ),
                                "error_msg": message,
                                "completed_at": utc_now(),
                            },
                        ],
                    }
                    store.finish_step(step_id, "failed", message)
            if not dry_run:
                _write_local_documents(self.data_root, all_records)
            status = "failed" if errors else "success"
            store.finish_run(context.run_id, status, "one or more OCR jobs failed" if errors else None)
            return {
                "status": status,
                "batch_id": batch_id,
                "run_id": context.run_id,
                "dry_run": dry_run,
                "backend": backend,
                "counts": counts,
                "errors": errors,
            }
        except Exception as exc:
            store.finish_run(context.run_id, "failed", f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if owns_store:
                store.close()

    def _output_dir(self, record: Mapping[str, Any], backend: str) -> Path:
        return (
            self.data_root
            / "parsed_md"
            / "financial"
            / str(record["work_uid"])
            / str(record["asset_uid"])
            / backend
        )

    def _archive_existing_output(
        self, output_dir: Path, backend: str, attempt_uid: str
    ) -> Path:
        """Copy a prior OCR result aside before a forced retry overwrites it."""

        archive_path = output_dir.parent / f"{backend}_history" / attempt_uid
        if archive_path.exists():
            raise FileExistsError(f"OCR archive already exists: {archive_path}")
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(output_dir, archive_path)
        return archive_path

    @staticmethod
    def _clear_materialized_ocr_output(output_dir: Path) -> None:
        """Clear current derived OCR files after a forced retry was archived."""

        for name in (
            "output.md",
            "result.json",
            "result.jsonl",
            "ocr_segments.json",
            "job.json",
            "attempt.json",
        ):
            (output_dir / name).unlink(missing_ok=True)
        for name in ("images", "pages", "segments"):
            path = output_dir / name
            if path.exists():
                shutil.rmtree(path)

    def _record_ocr_artifacts(
        self,
        store: StateStore,
        context: PipelineContext,
        record: Mapping[str, Any],
        output_dir: Path,
        backend: str,
    ) -> None:
        raw_artifact_id = artifact_uid("raw-pdf", record["asset_uid"])
        attempts = list(record.get("ocr_attempts") or ())
        latest_attempt = attempts[-1] if attempts else {}
        segment_manifest_artifact_id: str | None = None
        for name in (
            "input.json",
            "output.md",
            "result.json",
            "result.jsonl",
            "ocr_segments.json",
            "job.json",
            "attempt.json",
        ):
            path = output_dir / name
            if not path.is_file():
                continue
            store.upsert_artifact(
                context,
                {
                    "artifact_uid": artifact_uid(
                        "ocr-output", record["document_uid"], record["asset_uid"], backend, name
                    ),
                    "artifact_type": "ocr-output",
                    "document_uid": record["document_uid"],
                    "asset_uid": record["asset_uid"],
                    "path": context.relative_path(path),
                    "sha256": sha256_file(path),
                    "parent_artifact_uid": raw_artifact_id,
                    "metadata": {
                        "backend": backend,
                        "file_name": name,
                        "attempt_uid": latest_attempt.get("attempt_uid"),
                    },
                },
            )
            if name == "ocr_segments.json":
                segment_manifest_artifact_id = artifact_uid(
                    "ocr-output",
                    record["document_uid"],
                    record["asset_uid"],
                    backend,
                    name,
                )

        if segment_manifest_artifact_id is None:
            return
        manifest_path = output_dir / "ocr_segments.json"
        try:
            segment_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        segments = segment_manifest.get("segments") if isinstance(segment_manifest, Mapping) else None
        if not isinstance(segments, list):
            return
        for segment in segments:
            if not isinstance(segment, Mapping):
                continue
            result_path_value = str(segment.get("result_path") or "")
            result_path = output_dir / result_path_value
            segment_uid = str(segment.get("segment_uid") or "")
            if not segment_uid or not result_path.is_file():
                continue
            store.upsert_artifact(
                context,
                {
                    "artifact_uid": artifact_uid(
                        "ocr-output-segment",
                        record["document_uid"],
                        record["asset_uid"],
                        backend,
                        segment_uid,
                    ),
                    "artifact_type": "ocr-output-segment",
                    "document_uid": record["document_uid"],
                    "asset_uid": record["asset_uid"],
                    "path": context.relative_path(result_path),
                    "sha256": sha256_file(result_path),
                    "parent_artifact_uid": segment_manifest_artifact_id,
                    "metadata": {
                        "backend": backend,
                        "segment_uid": segment_uid,
                        "segment_index": segment.get("segment_index"),
                        "start_page": segment.get("start_page"),
                        "end_page": segment.get("end_page"),
                        "status": segment.get("status"),
                        "attempt_uid": latest_attempt.get("attempt_uid"),
                    },
                },
            )

    @staticmethod
    def _build_processor(backend: str) -> OcrProcessor:
        runtime = load_runtime_config()
        if backend == "local":
            if not runtime.local_paddle.api_url:
                raise ValueError(
                    "local OCR requires PADDLEOCR_LOCAL_API_URL or PADDLEOCR_API_URL"
                )
            from remote.paddle_client import PaddleOCRClient

            client = PaddleOCRClient(
                runtime.local_paddle.api_url,
                token=runtime.local_paddle.token,
                timeout_seconds=runtime.local_paddle.timeout_seconds,
                user_agent=runtime.local_paddle.user_agent,
            )
            return client.process_pdf
        from remote.paddle_cloud_client import PaddleOCRCloudClient

        return PaddleOCRCloudClient().process
