"""Deterministic, non-blocking quality checks for local financial OCR output."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from core.context import PipelineContext
from core.ids import stable_uid
from ingestion.local_documents import load_local_documents
from qc.validators import QCCheck
from spiders.downloader import sha256_file, utc_now
from spiders.http_client import write_jsonl_atomic
from storage.state_store import StateStore


OCR_QUALITY_MANIFEST = "ocr_quality.jsonl"
OCR_QUALITY_SCHEMA_VERSION = "financial-ocr-quality-v1"


def load_ocr_quality(data_root: Path | str) -> dict[str, dict[str, Any]]:
    """Load current OCR quality records indexed by immutable PDF asset UID."""

    path = Path(data_root) / "manifests" / OCR_QUALITY_MANIFEST
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
                raise ValueError(f"invalid OCR quality manifest line {line_number}") from exc
            if not isinstance(record, dict) or not record.get("asset_uid"):
                raise ValueError(
                    f"OCR quality manifest line {line_number} is missing asset_uid"
                )
            records[str(record["asset_uid"])] = record
    return records


def _load_result(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        return {}
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return result if isinstance(result, Mapping) else {}


def _result_page_count(result: Mapping[str, Any], fallback: object) -> int | None:
    pages = result.get("layoutParsingResults")
    if isinstance(pages, list):
        return len(pages)
    try:
        count = int(fallback)
    except (TypeError, ValueError):
        return None
    return count if count >= 0 else None


def _table_block_count(result: Mapping[str, Any]) -> int:
    count = 0
    for page in result.get("layoutParsingResults") or []:
        if not isinstance(page, Mapping):
            continue
        blocks = (
            page.get("prunedResult", {}).get("parsing_res_list", [])
            if isinstance(page.get("prunedResult"), Mapping)
            else []
        )
        for block in blocks:
            if isinstance(block, Mapping) and "table" in str(block.get("block_label") or "").lower():
                count += 1
    return count


def _metrics(text: str, result: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
    visible = [character for character in text if character.isprintable() and not character.isspace()]
    word_like = sum(
        character.isalnum() or "\u4e00" <= character <= "\u9fff" for character in visible
    )
    expected_pages = record.get("page_count")
    try:
        expected_page_count = int(expected_pages) if expected_pages is not None else None
    except (TypeError, ValueError):
        expected_page_count = None
    ocr_result = record.get("ocr_result")
    ocr_result_mapping = ocr_result if isinstance(ocr_result, Mapping) else {}
    return {
        "character_count": len(text),
        "nonempty_line_count": sum(1 for line in text.splitlines() if line.strip()),
        "replacement_character_count": text.count("\ufffd"),
        "visible_character_count": len(visible),
        "word_like_character_ratio": round(word_like / max(len(visible), 1), 4),
        "expected_page_count": expected_page_count,
        "parsed_page_count": _result_page_count(
            result, ocr_result_mapping.get("page_result_count")
        ),
        "table_block_count": _table_block_count(result),
    }


def _quality_checks(
    output_exists: bool,
    result_exists: bool,
    metrics: Mapping[str, Any],
) -> list[QCCheck]:
    character_count = int(metrics.get("character_count") or 0)
    expected_pages = metrics.get("expected_page_count")
    parsed_pages = metrics.get("parsed_page_count")
    minimum_characters = max(20, int(expected_pages or 1) * 20)
    coverage_ok = (
        expected_pages in (None, 0)
        or parsed_pages is not None
        and int(parsed_pages) >= int(expected_pages)
    )
    ratio = float(metrics.get("word_like_character_ratio") or 0)
    visible_count = int(metrics.get("visible_character_count") or 0)
    return [
        QCCheck(
            "ocr_output_present",
            "pass" if output_exists else "warn",
            "output.md is missing" if not output_exists else None,
        ),
        QCCheck(
            "ocr_result_present",
            "pass" if result_exists else "warn",
            "result.json is missing; page and table metrics are incomplete" if not result_exists else None,
        ),
        QCCheck(
            "ocr_text_nonempty",
            "pass" if character_count else "warn",
            "OCR text is empty" if not character_count else None,
            character_count,
        ),
        QCCheck(
            "ocr_text_sufficient",
            "pass" if character_count >= minimum_characters else "warn",
            (
                f"OCR text has {character_count} characters; expected at least {minimum_characters}"
                if character_count < minimum_characters
                else None
            ),
            {"character_count": character_count, "minimum_characters": minimum_characters},
        ),
        QCCheck(
            "ocr_page_coverage",
            "pass" if coverage_ok else "warn",
            (
                f"OCR returned {parsed_pages} pages for a {expected_pages}-page PDF"
                if not coverage_ok
                else None
            ),
            {"expected_page_count": expected_pages, "parsed_page_count": parsed_pages},
        ),
        QCCheck(
            "ocr_replacement_characters",
            "pass" if not metrics.get("replacement_character_count") else "warn",
            "OCR text contains Unicode replacement characters"
            if metrics.get("replacement_character_count")
            else None,
            metrics.get("replacement_character_count"),
        ),
        QCCheck(
            "ocr_text_character_ratio",
            "pass" if not visible_count or ratio >= 0.2 else "warn",
            "OCR text contains an unusually low ratio of letters, digits or CJK characters"
            if visible_count and ratio < 0.2
            else None,
            ratio,
        ),
    ]


def assess_ocr_quality(data_root: Path | str, record: Mapping[str, Any]) -> dict[str, Any]:
    """Calculate reproducible OCR metrics for one imported financial PDF."""

    root = Path(data_root)
    output_dir_value = str(record.get("ocr_output_dir") or "")
    output_dir = root / output_dir_value
    output_path = output_dir / "output.md"
    result_path = output_dir / "result.json"
    output_exists = output_path.is_file()
    result_exists = result_path.is_file()
    try:
        text = output_path.read_text(encoding="utf-8") if output_exists else ""
    except OSError:
        text = ""
        output_exists = False
    result = _load_result(result_path)
    metrics = _metrics(text, result, record)
    input_hash = stable_uid(
        "financial-ocr-quality-input",
        record.get("raw_file_hash"),
        record.get("ocr_backend"),
        sha256_file(output_path) if output_exists else "missing-output",
        sha256_file(result_path) if result_exists else "missing-result",
    )
    checks = _quality_checks(output_exists, result_exists, metrics)
    warnings = [check for check in checks if check.status == "warn"]
    return {
        "schema_version": OCR_QUALITY_SCHEMA_VERSION,
        "asset_uid": record.get("asset_uid"),
        "document_uid": record.get("document_uid"),
        "batch_ids": sorted(
            {
                str(value)
                for value in record.get("batch_ids") or (record.get("batch_id"),)
                if value
            }
        ),
        "ocr_backend": record.get("ocr_backend"),
        "ocr_output_dir": output_dir_value,
        "ocr_input_hash": input_hash,
        "metrics": metrics,
        "qc_checks": [check.as_mapping() for check in checks],
        "status": "needs_review" if warnings else "pass",
        "generated_at": utc_now(),
    }


class OcrQualityRunner:
    """Materialize non-blocking OCR quality records for one local batch."""

    def __init__(self, data_root: Path | str, *, state_store: StateStore | None = None) -> None:
        self.data_root = Path(data_root)
        self.state_store = state_store

    def run(
        self, *, batch_id: str, dry_run: bool = False, force: bool = False
    ) -> dict[str, Any]:
        local_records = load_local_documents(self.data_root)
        candidates = [
            record
            for record in local_records.values()
            if (
                batch_id in set(record.get("batch_ids") or (record.get("batch_id"),))
                and record.get("status") == "success"
                and record.get("ocr_status") == "success"
            )
        ]
        if not candidates:
            raise ValueError(f"batch {batch_id!r} has no OCR-complete PDFs for quality checks")
        context = PipelineContext.create(
            self.data_root, batch_id=batch_id, config_version="ocr-quality-v1"
        )
        store = self.state_store or StateStore(self.data_root / "state" / "pipeline.db")
        owns_store = self.state_store is None
        existing = load_ocr_quality(self.data_root)
        all_records = dict(existing)
        counts = {"processed": 0, "skipped": 0, "preview": 0, "failed": 0}
        quality_statuses: Counter[str] = Counter()
        warning_count = 0
        errors: list[dict[str, str]] = []
        try:
            store.start_run(context, dry_run=dry_run)
            for local_record in sorted(candidates, key=lambda item: str(item["asset_uid"])):
                asset_uid = str(local_record["asset_uid"])
                step_id = store.start_step(
                    context, "ocr-quality", asset_uid, metadata={"ocr_backend": local_record.get("ocr_backend")}
                )
                try:
                    quality = assess_ocr_quality(self.data_root, local_record)
                    previous = existing.get(asset_uid)
                    if previous and previous.get("ocr_input_hash") == quality["ocr_input_hash"] and not force:
                        quality = previous
                        disposition = "skipped"
                    else:
                        disposition = "preview" if dry_run else "processed"
                    counts[disposition] += 1
                    quality_statuses[str(quality["status"])] += 1
                    warning_count += sum(
                        check.get("status") == "warn" for check in quality.get("qc_checks") or []
                    )
                    if not dry_run:
                        store.upsert_ocr_quality(context, quality)
                        store.record_qc(
                            context,
                            asset_uid,
                            "ocr_quality",
                            list(quality.get("qc_checks") or ()),
                        )
                        all_records[asset_uid] = quality
                    store.finish_step(step_id, "success")
                except (OSError, ValueError, RuntimeError) as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    counts["failed"] += 1
                    errors.append({"asset_uid": asset_uid, "error_msg": message})
                    store.finish_step(step_id, "failed", message)
            if not dry_run:
                write_jsonl_atomic(
                    self.data_root / "manifests" / OCR_QUALITY_MANIFEST,
                    [all_records[asset_uid] for asset_uid in sorted(all_records)],
                )
            status = "failed" if errors else "success"
            store.finish_run(
                context.run_id, status, "one or more OCR quality checks failed" if errors else None
            )
            return {
                "status": status,
                "batch_id": batch_id,
                "run_id": context.run_id,
                "dry_run": dry_run,
                "counts": counts,
                "quality_statuses": dict(sorted(quality_statuses.items())),
                "warning_count": warning_count,
                "errors": errors,
            }
        except Exception as exc:
            store.finish_run(context.run_id, "failed", f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if owns_store:
                store.close()


class OcrQualityReviewService:
    """List batch assets whose OCR requires local inspection or a retry."""

    def __init__(self, data_root: Path | str) -> None:
        self.data_root = Path(data_root)

    def review(self, *, batch_id: str) -> dict[str, Any]:
        local_records = load_local_documents(self.data_root)
        quality_records = load_ocr_quality(self.data_root)
        items: list[dict[str, Any]] = []
        batch_records = [
            record
            for record in local_records.values()
            if batch_id in set(record.get("batch_ids") or (record.get("batch_id"),))
        ]
        for local_record in sorted(batch_records, key=lambda item: str(item["asset_uid"])):
            asset_uid = str(local_record["asset_uid"])
            quality = quality_records.get(asset_uid)
            if quality is None:
                items.append(
                    {
                        "asset_uid": asset_uid,
                        "document_uid": local_record.get("document_uid"),
                        "status": "not_checked",
                        "warnings": [
                            {"check_name": "ocr_quality_not_generated", "message": "run ocr-quality first"}
                        ],
                        "ocr_attempts": local_record.get("ocr_attempts") or [],
                    }
                )
                continue
            warnings = [
                {"check_name": check.get("check_name"), "message": check.get("message")}
                for check in quality.get("qc_checks") or []
                if isinstance(check, Mapping) and check.get("status") == "warn"
            ]
            if warnings:
                items.append(
                    {
                        "asset_uid": asset_uid,
                        "document_uid": quality.get("document_uid"),
                        "status": quality.get("status"),
                        "warnings": warnings,
                        "metrics": quality.get("metrics") or {},
                        "ocr_attempts": local_record.get("ocr_attempts") or [],
                    }
                )
        return {"batch_id": batch_id, "asset_count": len(batch_records), "review_count": len(items), "items": items}


__all__ = [
    "OCR_QUALITY_MANIFEST",
    "OcrQualityReviewService",
    "OcrQualityRunner",
    "assess_ocr_quality",
    "load_ocr_quality",
]
