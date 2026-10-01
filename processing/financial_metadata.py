"""Build normalized, non-blocking metadata records for local financial PDFs."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from core.context import PipelineContext
from core.ids import stable_uid
from ingestion.local_documents import load_local_documents
from qc.validators import QCCheck, validate_financial_metadata
from spiders.downloader import utc_now
from spiders.http_client import write_jsonl_atomic
from storage.state_store import StateStore


FINANCIAL_METADATA_MANIFEST = "financial_metadata.jsonl"
FINANCIAL_METADATA_OVERRIDE_MANIFEST = "financial_metadata_overrides.jsonl"
FINANCIAL_METADATA_OVERRIDE_AUDIT_MANIFEST = "financial_metadata_override_audit.jsonl"
FINANCIAL_REPORT_VERSION_MANIFEST = "financial_report_versions.jsonl"
FINANCIAL_REPORT_VERSION_AUDIT_MANIFEST = "financial_report_version_selection_audit.jsonl"
METADATA_SCHEMA_VERSION = "financial-document-metadata-v1"
METADATA_EDITABLE_FIELDS = (
    "stock_code",
    "company_name",
    "report_year",
    "report_period",
    "report_type",
    "announcement_date",
    "title",
    "source_name",
    "source_id",
    "language",
    "document_variant",
)
_STOCK_CODE_PATTERN = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_YEAR_PATTERN = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_DATE_PATTERN = re.compile(
    r"(?<!\d)(20\d{2})[./\-\u5e74](\d{1,2})[./\-\u6708](\d{1,2})(?:\u65e5)?"
)
_REPORT_TYPE_MARKERS = {
    "annual": ("annual report", "\u5e74\u5ea6\u62a5\u544a", "\u5e74\u62a5"),
    "semiannual": (
        "semiannual",
        "semi-annual",
        "interim report",
        "\u534a\u5e74\u5ea6\u62a5\u544a",
        "\u4e2d\u671f\u62a5\u544a",
        "\u534a\u5e74\u62a5",
    ),
    "quarterly": ("quarterly report", "\u5b63\u5ea6\u62a5\u544a", "\u5b63\u62a5"),
    "research": ("research report", "\u7814\u7a76\u62a5\u544a"),
}
_REPORT_TYPE_ALIASES = {
    "annual": "annual",
    "yearly": "annual",
    "\u5e74\u62a5": "annual",
    "\u5e74\u5ea6\u62a5\u544a": "annual",
    "semiannual": "semiannual",
    "semi-annual": "semiannual",
    "interim": "semiannual",
    "\u534a\u5e74\u62a5": "semiannual",
    "\u534a\u5e74\u5ea6\u62a5\u544a": "semiannual",
    "quarterly": "quarterly",
    "\u5b63\u62a5": "quarterly",
    "\u5b63\u5ea6\u62a5\u544a": "quarterly",
    "research": "research",
    "\u7814\u7a76\u62a5\u544a": "research",
}
_REQUIRED_FIELDS = (
    "document_uid",
    "asset_uid",
    "stock_code",
    "company_name",
    "report_year",
    "report_type",
    "title",
    "source_name",
    "language",
    "raw_file_hash",
)


def load_financial_metadata(data_root: Path | str) -> dict[str, dict[str, Any]]:
    """Load normalized local metadata indexed by immutable asset UID."""

    path = Path(data_root) / "manifests" / FINANCIAL_METADATA_MANIFEST
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
                raise ValueError(
                    f"invalid financial metadata manifest line {line_number}"
                ) from exc
            if not isinstance(record, dict) or not record.get("asset_uid"):
                raise ValueError(
                    f"financial metadata manifest line {line_number} is missing asset_uid"
                )
            records[str(record["asset_uid"])] = record
    return records


def load_financial_metadata_overrides(data_root: Path | str) -> dict[str, dict[str, Any]]:
    """Load the human-readable projection of the current manual overrides."""

    path = Path(data_root) / "manifests" / FINANCIAL_METADATA_OVERRIDE_MANIFEST
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
                raise ValueError(
                    f"invalid financial metadata override manifest line {line_number}"
                ) from exc
            overrides = record.get("overrides") if isinstance(record, dict) else None
            if (
                not isinstance(record, dict)
                or not record.get("asset_uid")
                or not isinstance(overrides, dict)
            ):
                raise ValueError(
                    f"financial metadata override manifest line {line_number} is invalid"
                )
            records[str(record["asset_uid"])] = record
    return records


def _as_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _as_year(value: object) -> int | None:
    try:
        year = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return year if 1900 <= year <= 2100 else None


def _as_date(value: object) -> str | None:
    text = _as_text(value)
    if not text:
        return None
    match = _DATE_PATTERN.search(text)
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return None


def _report_type(value: object) -> str | None:
    text = _as_text(value)
    if not text:
        return None
    normalized = text.lower().replace("_", "-")
    return _REPORT_TYPE_ALIASES.get(normalized)


def _detect_language(text: str) -> str:
    cjk_count = sum(1 for character in text[:4000] if "\u4e00" <= character <= "\u9fff")
    return "zh-CN" if cjk_count / max(len(text[:4000]), 1) > 0.15 else "en"


def _ocr_heading(data_root: Path, record: Mapping[str, Any]) -> str | None:
    output_dir = _as_text(record.get("ocr_output_dir"))
    if not output_dir:
        return None
    path = data_root / output_dir / "output.md"
    if not path.is_file():
        return None
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip().lstrip("#").strip()
            if line and len(line) <= 240:
                return line
    except OSError:
        return None
    return None


def _source_value(
    record: Mapping[str, Any], field_name: str, sources: dict[str, str]
) -> object | None:
    imported = record.get("import_metadata")
    if isinstance(imported, Mapping) and _as_text(imported.get(field_name)):
        sources[field_name] = "explicit_import"
        return imported[field_name]
    source_metadata = record.get("source_metadata")
    if isinstance(source_metadata, Mapping) and _as_text(source_metadata.get(field_name)):
        sources[field_name] = "source_manifest"
        return source_metadata[field_name]
    if _as_text(record.get(field_name)):
        sources[field_name] = "source_manifest"
        return record[field_name]
    return None


def _text_candidates(data_root: Path, record: Mapping[str, Any]) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    title = _as_text(record.get("title"))
    if title:
        candidates.append(("title", title))
    source_path = _as_text(record.get("source_path")) or _as_text(record.get("raw_path"))
    if source_path:
        candidates.append(("filename", Path(source_path).stem))
    heading = _ocr_heading(data_root, record)
    if heading:
        candidates.append(("ocr_first_page", heading))
    return candidates


def _infer_stock_code(candidates: list[tuple[str, str]]) -> tuple[str | None, str | None]:
    for source, text in candidates:
        match = _STOCK_CODE_PATTERN.search(text)
        if match:
            return match.group(1), source
    return None, None


def _infer_year(candidates: list[tuple[str, str]]) -> tuple[int | None, str | None]:
    for source, text in candidates:
        match = _YEAR_PATTERN.search(text)
        if match:
            return int(match.group(1)), source
    return None, None


def _infer_report_type(candidates: list[tuple[str, str]]) -> tuple[str | None, str | None]:
    for source, text in candidates:
        lowered = text.lower()
        for report_type, markers in _REPORT_TYPE_MARKERS.items():
            if any(marker in lowered for marker in markers):
                return report_type, source
    return None, None


def _infer_date(candidates: list[tuple[str, str]]) -> tuple[str | None, str | None]:
    for source, text in candidates:
        value = _as_date(text)
        if value:
            return value, source
    return None, None


def _infer_company_name(candidates: list[tuple[str, str]]) -> tuple[str | None, str | None]:
    for source, text in candidates:
        match = _YEAR_PATTERN.search(text)
        if not match:
            continue
        prefix = text[: match.start()]
        prefix = _STOCK_CODE_PATTERN.sub("", prefix)
        prefix = prefix.strip(" _-./()[]{}")
        has_letters = bool(re.search(r"[A-Za-z]", prefix))
        has_cjk = any("\u4e00" <= character <= "\u9fff" for character in prefix)
        if 2 <= len(prefix) <= 100 and (has_cjk or has_letters):
            return prefix, source
    return None, None


def _set_inferred(
    values: dict[str, Any],
    sources: dict[str, str],
    field_name: str,
    inferred: tuple[Any | None, str | None],
) -> None:
    value, source = inferred
    if value is not None and values.get(field_name) in (None, ""):
        values[field_name] = value
        sources[field_name] = source or "inferred"


def _manual_value(field_name: str, value: object) -> Any:
    """Keep manual input visible even when it still needs a QC correction."""

    text = _as_text(value)
    if text is None:
        return None
    if field_name == "report_year":
        return _as_year(text) or text
    if field_name == "report_type":
        return _report_type(text) or text.lower()
    if field_name == "announcement_date":
        return _as_date(text) or text
    return text


def _apply_manual_overrides(
    values: dict[str, Any], sources: dict[str, str], overrides: Mapping[str, Any] | None
) -> dict[str, Any]:
    applied: dict[str, Any] = {}
    if not isinstance(overrides, Mapping):
        return applied
    for field_name in METADATA_EDITABLE_FIELDS:
        if field_name not in overrides:
            continue
        value = _manual_value(field_name, overrides[field_name])
        if value is None:
            continue
        values[field_name] = value
        sources[field_name] = "manual_override"
        applied[field_name] = value
    return applied


def financial_report_group_uid(record: Mapping[str, Any]) -> str | None:
    """Return one logical report group across immutable file versions."""

    stock_code = _as_text(record.get("stock_code"))
    report_year = _as_year(record.get("report_year"))
    report_type = _report_type(record.get("report_type"))
    language = _as_text(record.get("language"))
    if not (
        stock_code
        and _STOCK_CODE_PATTERN.fullmatch(stock_code)
        and report_year
        and report_type
        and language
    ):
        return None
    return stable_uid(
        "financial-report-version-group", stock_code, report_year, report_type, language
    )


def normalize_financial_metadata(
    data_root: Path | str,
    record: Mapping[str, Any],
    *,
    manual_overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize one asset with manually reviewed values as the highest authority."""

    root = Path(data_root)
    sources: dict[str, str] = {}
    values: dict[str, Any] = {
        "document_uid": _source_value(record, "document_uid", sources),
        "asset_uid": _source_value(record, "asset_uid", sources),
        "stock_code": _as_text(_source_value(record, "stock_code", sources)),
        "company_name": _as_text(_source_value(record, "company_name", sources)),
        "report_year": _as_year(_source_value(record, "report_year", sources)),
        "report_period": _as_text(_source_value(record, "report_period", sources)),
        "report_type": _report_type(_source_value(record, "report_type", sources)),
        "announcement_date": _as_date(_source_value(record, "announcement_date", sources)),
        "title": _as_text(_source_value(record, "title", sources)),
        "source_name": _as_text(_source_value(record, "source_name", sources)),
        "source_id": _as_text(_source_value(record, "source_id", sources)),
        "language": _as_text(_source_value(record, "language", sources)),
        "document_variant": _as_text(_source_value(record, "document_variant", sources)),
        "raw_file_hash": _as_text(_source_value(record, "raw_file_hash", sources)),
    }
    candidates = _text_candidates(root, record)
    _set_inferred(values, sources, "stock_code", _infer_stock_code(candidates))
    _set_inferred(values, sources, "company_name", _infer_company_name(candidates))
    _set_inferred(values, sources, "report_year", _infer_year(candidates))
    _set_inferred(values, sources, "report_type", _infer_report_type(candidates))
    _set_inferred(values, sources, "announcement_date", _infer_date(candidates))
    applied_overrides = _apply_manual_overrides(values, sources, manual_overrides)

    if not values["report_period"] and values["report_year"]:
        period_suffix = {"annual": "", "semiannual": "-H1"}.get(values["report_type"])
        if period_suffix is not None:
            values["report_period"] = f"{values['report_year']}{period_suffix}"
            sources["report_period"] = "derived_report_type"
    if not values["language"]:
        language_sample = "\n".join(text for _source, text in candidates)
        values["language"] = _detect_language(language_sample)
        sources["language"] = "title_or_ocr"
    if not values["document_variant"]:
        values["document_variant"] = "full"
        sources["document_variant"] = "default"

    metadata = {
        "schema_version": METADATA_SCHEMA_VERSION,
        **values,
        "batch_ids": sorted(
            {
                str(value)
                for value in record.get("batch_ids") or (record.get("batch_id"),)
                if value
            }
        ),
        "ocr_backend": _as_text(record.get("ocr_backend")),
        "metadata_sources": sources,
        "manual_overrides": applied_overrides,
        "generated_at": utc_now(),
    }
    metadata["report_group_uid"] = financial_report_group_uid(metadata)
    metadata["metadata_hash"] = stable_uid(
        "financial-document-metadata",
        json.dumps(
            {key: value for key, value in metadata.items() if key != "generated_at"},
            ensure_ascii=False,
            sort_keys=True,
        ),
    )
    return metadata


def _metadata_key(record: Mapping[str, Any]) -> tuple[str, int, str, str, str] | None:
    stock_code = _as_text(record.get("stock_code"))
    year = _as_year(record.get("report_year"))
    report_type = _report_type(record.get("report_type"))
    language = _as_text(record.get("language"))
    variant = _as_text(record.get("document_variant"))
    if not all((stock_code, year, report_type, language, variant)):
        return None
    return stock_code, year, report_type, language, variant


def _quality_checks(
    record: Mapping[str, Any], duplicate_assets: list[str]
) -> list[QCCheck]:
    checks = validate_financial_metadata(record)
    checks.append(
        QCCheck(
            "metadata_version_conflict",
            "warn" if duplicate_assets else "pass",
            (
                "multiple assets share the same logical report metadata: "
                + ", ".join(duplicate_assets)
                if duplicate_assets
                else None
            ),
            {"asset_uids": duplicate_assets},
        )
    )
    return checks


def parse_metadata_override_assignments(assignments: list[str]) -> dict[str, Any]:
    """Parse repeatable CLI ``FIELD=VALUE`` assignments into approved fields."""

    overrides: dict[str, Any] = {}
    for assignment in assignments:
        if "=" not in assignment:
            raise ValueError("metadata overrides must use FIELD=VALUE")
        field_name, raw_value = assignment.split("=", 1)
        field_name = field_name.strip()
        value = _manual_value(field_name, raw_value)
        if field_name not in METADATA_EDITABLE_FIELDS:
            choices = ", ".join(METADATA_EDITABLE_FIELDS)
            raise ValueError(f"metadata field {field_name!r} is not editable; choose from {choices}")
        if value is None:
            raise ValueError(f"metadata override {field_name!r} cannot be empty")
        if field_name in overrides:
            raise ValueError(f"metadata field {field_name!r} was provided more than once")
        overrides[field_name] = value
    if not overrides:
        raise ValueError("at least one metadata override is required")
    return overrides


class FinancialMetadataRunner:
    """Materialize standard metadata and QC hints without blocking downstream stages."""

    def __init__(
        self, data_root: Path | str, *, state_store: StateStore | None = None
    ) -> None:
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
            raise ValueError(
                f"batch {batch_id!r} has no OCR-complete local PDFs ready for metadata"
            )

        context = PipelineContext.create(
            self.data_root, batch_id=batch_id, config_version="financial-metadata-v1"
        )
        store = self.state_store or StateStore(self.data_root / "state" / "pipeline.db")
        owns_store = self.state_store is None
        existing = load_financial_metadata(self.data_root)
        manual_overrides = store.list_financial_metadata_overrides()
        normalized: dict[str, dict[str, Any]] = {}
        counts = {"processed": 0, "skipped": 0, "preview": 0, "failed": 0}
        errors: list[dict[str, str]] = []
        try:
            store.start_run(context, dry_run=dry_run)
            for local_record in sorted(candidates, key=lambda item: str(item["asset_uid"])):
                asset_uid = str(local_record["asset_uid"])
                step_id = store.start_step(
                    context, "financial-metadata", asset_uid, metadata={"document_uid": local_record.get("document_uid")}
                )
                try:
                    override_record = manual_overrides.get(asset_uid, {})
                    metadata = normalize_financial_metadata(
                        self.data_root,
                        local_record,
                        manual_overrides=override_record.get("overrides"),
                    )
                    previous = existing.get(asset_uid)
                    if (
                        previous
                        and previous.get("metadata_hash") == metadata["metadata_hash"]
                        and not force
                    ):
                        normalized[asset_uid] = previous
                        disposition = "skipped"
                    else:
                        normalized[asset_uid] = metadata
                        disposition = "preview" if dry_run else "processed"
                    counts[disposition] += 1
                    store.finish_step(step_id, "success")
                except (OSError, ValueError) as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    counts["failed"] += 1
                    errors.append({"asset_uid": asset_uid, "error_msg": message})
                    store.finish_step(step_id, "failed", message)

            all_metadata = {**existing, **normalized}
            grouped_assets: dict[tuple[str, int, str, str, str], list[str]] = defaultdict(list)
            for asset_uid, record in all_metadata.items():
                key = _metadata_key(record)
                if key:
                    grouped_assets[key].append(asset_uid)

            warning_count = 0
            status_counts: Counter[str] = Counter()
            for asset_uid, metadata in normalized.items():
                key = _metadata_key(metadata)
                duplicate_assets = sorted(
                    other for other in (grouped_assets.get(key, []) if key else []) if other != asset_uid
                )
                checks = _quality_checks(metadata, duplicate_assets)
                metadata["qc_checks"] = [check.as_mapping() for check in checks]
                metadata["missing_fields"] = [
                    field_name for field_name in _REQUIRED_FIELDS if not metadata.get(field_name)
                ]
                metadata["status"] = "complete" if not metadata["missing_fields"] else "needs_review"
                status_counts[metadata["status"]] += 1
                warning_count += sum(check.status == "warn" for check in checks)
                if not dry_run:
                    store.upsert_financial_metadata(context, metadata)
                    store.record_qc(
                        context,
                        asset_uid,
                        "financial_metadata",
                        [check.as_mapping() for check in checks],
                    )
                    all_metadata[asset_uid] = metadata

            if not dry_run:
                write_jsonl_atomic(
                    self.data_root / "manifests" / FINANCIAL_METADATA_MANIFEST,
                    [all_metadata[asset_uid] for asset_uid in sorted(all_metadata)],
                )
                version_records = store.sync_financial_report_versions(all_metadata.values())
                _write_financial_report_version_projections(
                    self.data_root, store, version_records
                )
            else:
                version_records = []
            status = "failed" if errors else "success"
            store.finish_run(
                context.run_id,
                status,
                "one or more metadata records failed" if errors else None,
            )
            return {
                "status": status,
                "batch_id": batch_id,
                "run_id": context.run_id,
                "dry_run": dry_run,
                "counts": counts,
                "metadata_statuses": dict(sorted(status_counts.items())),
                "version_record_count": len(version_records),
                "warning_count": warning_count,
                "errors": errors,
            }
        except Exception as exc:
            store.finish_run(context.run_id, "failed", f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if owns_store:
                store.close()


class FinancialMetadataReviewService:
    """Review and correct generated metadata without re-running OCR or governance."""

    def __init__(self, data_root: Path | str) -> None:
        self.data_root = Path(data_root)

    def review(self, *, batch_id: str) -> dict[str, Any]:
        """List only the batch assets that still have metadata warnings or gaps."""

        local_records = load_local_documents(self.data_root)
        batch_records = [
            record
            for record in local_records.values()
            if batch_id in set(record.get("batch_ids") or (record.get("batch_id"),))
        ]
        metadata_records = load_financial_metadata(self.data_root)
        with StateStore(self.data_root / "state" / "pipeline.db") as store:
            overrides = store.list_financial_metadata_overrides()

        items: list[dict[str, Any]] = []
        for record in sorted(batch_records, key=lambda item: str(item["asset_uid"])):
            asset_uid = str(record["asset_uid"])
            metadata = metadata_records.get(asset_uid)
            if metadata is None:
                items.append(
                    {
                        "asset_uid": asset_uid,
                        "document_uid": record.get("document_uid"),
                        "status": "not_generated",
                        "missing_fields": list(_REQUIRED_FIELDS),
                        "warnings": [
                            {
                                "check_name": "metadata_not_generated",
                                "message": "run the metadata stage before review",
                            }
                        ],
                        "manual_overrides": overrides.get(asset_uid, {}).get("overrides", {}),
                    }
                )
                continue
            warnings = [
                {
                    "check_name": check.get("check_name"),
                    "message": check.get("message"),
                }
                for check in metadata.get("qc_checks") or []
                if isinstance(check, Mapping) and check.get("status") == "warn"
            ]
            if metadata.get("status") == "needs_review" or warnings:
                items.append(
                    {
                        "asset_uid": asset_uid,
                        "document_uid": metadata.get("document_uid"),
                        "title": metadata.get("title"),
                        "status": metadata.get("status"),
                        "missing_fields": metadata.get("missing_fields") or [],
                        "warnings": warnings,
                        "manual_overrides": overrides.get(asset_uid, {}).get("overrides", {}),
                    }
                )
        return {
            "batch_id": batch_id,
            "asset_count": len(batch_records),
            "review_count": len(items),
            "items": items,
        }

    def apply_override(
        self,
        *,
        batch_id: str,
        asset_uid: str,
        overrides: Mapping[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        """Persist an audited manual correction then regenerate local metadata only."""

        reason_text = _as_text(reason)
        if not reason_text:
            raise ValueError("a non-empty override reason is required")
        approved_overrides = parse_metadata_override_assignments(
            [f"{field_name}={value}" for field_name, value in overrides.items()]
        )
        local_record = load_local_documents(self.data_root).get(asset_uid)
        if local_record is None:
            raise ValueError(f"local asset does not exist: {asset_uid}")
        if batch_id not in set(local_record.get("batch_ids") or (local_record.get("batch_id"),)):
            raise ValueError(f"asset {asset_uid!r} is not part of batch {batch_id!r}")
        if local_record.get("ocr_status") != "success":
            raise ValueError(f"asset {asset_uid!r} has not completed OCR")

        metadata_records = load_financial_metadata(self.data_root)
        current_metadata = metadata_records.get(asset_uid, {})
        with StateStore(self.data_root / "state" / "pipeline.db") as store:
            current_override = store.list_financial_metadata_overrides().get(asset_uid, {})
            merged_overrides = {
                **dict(current_override.get("overrides") or {}),
                **approved_overrides,
            }
            field_changes = [
                {
                    "field_name": field_name,
                    "before": current_metadata.get(field_name),
                    "after": value,
                }
                for field_name, value in approved_overrides.items()
                if current_metadata.get(field_name) != value
            ]
            if not field_changes:
                raise ValueError("the supplied overrides do not change the current metadata")
            audit_event = store.record_financial_metadata_override(
                asset_uid=asset_uid,
                document_uid=str(local_record["document_uid"]),
                batch_id=batch_id,
                overrides=merged_overrides,
                reason=reason_text,
                field_changes=field_changes,
            )
            current_overrides = store.list_financial_metadata_overrides()
            audit_records = store.list_financial_metadata_override_audits()
            write_jsonl_atomic(
                self.data_root / "manifests" / FINANCIAL_METADATA_OVERRIDE_MANIFEST,
                [current_overrides[key] for key in sorted(current_overrides)],
            )
            write_jsonl_atomic(
                self.data_root / "manifests" / FINANCIAL_METADATA_OVERRIDE_AUDIT_MANIFEST,
                audit_records,
            )

        metadata_result = FinancialMetadataRunner(self.data_root).run(batch_id=batch_id)
        return {
            "status": "success",
            "override": audit_event,
            "metadata": metadata_result,
        }


def _write_financial_report_version_projections(
    data_root: Path, store: StateStore, version_records: list[Mapping[str, Any]] | None = None
) -> None:
    """Export current version state and its immutable selection audit for local review."""

    records = list(version_records or store.list_financial_report_versions())
    group_uids = [str(record["report_group_uid"]) for record in records]
    write_jsonl_atomic(
        data_root / "manifests" / FINANCIAL_REPORT_VERSION_MANIFEST,
        records,
    )
    write_jsonl_atomic(
        data_root / "manifests" / FINANCIAL_REPORT_VERSION_AUDIT_MANIFEST,
        store.list_financial_report_version_selection_audits(group_uids),
    )


class FinancialReportVersionService:
    """Expose deterministic local review and selection for report file versions."""

    def __init__(self, data_root: Path | str) -> None:
        self.data_root = Path(data_root)

    def review(self, *, batch_id: str) -> dict[str, Any]:
        """List version groups in the batch and highlight groups needing a choice."""

        local_records = load_local_documents(self.data_root)
        batch_records = {
            str(record["asset_uid"]): record
            for record in local_records.values()
            if batch_id in set(record.get("batch_ids") or (record.get("batch_id"),))
        }
        metadata_records = load_financial_metadata(self.data_root)
        with StateStore(self.data_root / "state" / "pipeline.db") as store:
            version_records = store.list_financial_report_versions(batch_records)

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for version in version_records:
            asset_uid = str(version["asset_uid"])
            metadata = metadata_records.get(asset_uid, {})
            source = batch_records.get(asset_uid, {})
            grouped[str(version["report_group_uid"])].append(
                {
                    **version,
                    "title": metadata.get("title") or source.get("title"),
                    "raw_file_hash": metadata.get("raw_file_hash") or source.get("raw_file_hash"),
                    "page_count": source.get("page_count"),
                    "imported_at": source.get("imported_at"),
                }
            )

        groups: list[dict[str, Any]] = []
        for group_uid, versions in sorted(grouped.items()):
            ordered_versions = sorted(versions, key=lambda item: (item["created_at"], item["asset_uid"]))
            active = next(
                (item for item in ordered_versions if item["version_status"] == "active"), None
            )
            requires_review = len(ordered_versions) > 1 and (
                active is None or active.get("selection_reason") != "manual_review"
            )
            groups.append(
                {
                    "report_group_uid": group_uid,
                    "version_count": len(ordered_versions),
                    "active_asset_uid": active.get("asset_uid") if active else None,
                    "selection_reason": active.get("selection_reason") if active else None,
                    "requires_review": requires_review,
                    "versions": ordered_versions,
                }
            )
        review_groups = [group for group in groups if group["requires_review"]]
        return {
            "batch_id": batch_id,
            "group_count": len(groups),
            "review_count": len(review_groups),
            "groups": groups,
        }

    def select(
        self,
        *,
        batch_id: str,
        report_group_uid: str,
        asset_uid: str,
        reason: str,
    ) -> dict[str, Any]:
        """Select one local asset as the effective version for future deliveries."""

        reason_text = _as_text(reason)
        if not reason_text:
            raise ValueError("a non-empty version-selection reason is required")
        local_record = load_local_documents(self.data_root).get(asset_uid)
        if local_record is None:
            raise ValueError(f"local asset does not exist: {asset_uid}")
        if batch_id not in set(local_record.get("batch_ids") or (local_record.get("batch_id"),)):
            raise ValueError(f"asset {asset_uid!r} is not part of batch {batch_id!r}")
        with StateStore(self.data_root / "state" / "pipeline.db") as store:
            audit_event = store.select_financial_report_version(
                report_group_uid=report_group_uid,
                asset_uid=asset_uid,
                batch_id=batch_id,
                reason=reason_text,
            )
            _write_financial_report_version_projections(self.data_root, store)
        return {"status": "success", "selection": audit_event}


__all__ = [
    "FINANCIAL_METADATA_MANIFEST",
    "FINANCIAL_METADATA_OVERRIDE_AUDIT_MANIFEST",
    "FINANCIAL_METADATA_OVERRIDE_MANIFEST",
    "FINANCIAL_REPORT_VERSION_AUDIT_MANIFEST",
    "FINANCIAL_REPORT_VERSION_MANIFEST",
    "FinancialMetadataRunner",
    "FinancialMetadataReviewService",
    "FinancialReportVersionService",
    "financial_report_group_uid",
    "load_financial_metadata",
    "load_financial_metadata_overrides",
    "normalize_financial_metadata",
    "parse_metadata_override_assignments",
]
