"""Deterministic QC checks for discovered candidates and raw PDF assets."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

from spiders.downloader import inspect_pdf, sha256_file
from spiders.source_adapters import clean_title, report_year_from_title


STOCK_CODE_PATTERN = re.compile(r"^\d{6}$")
METADATA_YEAR_PATTERN = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
METADATA_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SUPPORTED_FINANCIAL_REPORT_TYPES = {"annual", "semiannual", "quarterly", "research"}
REQUIRED_FINANCIAL_METADATA_FIELDS = (
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


@dataclass(frozen=True)
class QCCheck:
    check_name: str
    status: str
    message: str | None = None
    value: Any = None

    def as_mapping(self) -> dict[str, Any]:
        return asdict(self)


def qc_passed(checks: Iterable[QCCheck]) -> bool:
    return all(check.status != "fail" for check in checks)


def validate_candidate(candidate: Any) -> list[QCCheck]:
    """Validate normalized discovery metadata before download."""

    title = clean_title(candidate.title)
    title_year = report_year_from_title(title)
    checks = [
        QCCheck(
            "stock_code_format",
            "pass" if STOCK_CODE_PATTERN.fullmatch(candidate.stock_code) else "fail",
            value=candidate.stock_code,
        ),
        QCCheck(
            "source_url_https",
            "pass" if candidate.source_url.startswith("https://") else "warn",
            "source URL is not HTTPS" if not candidate.source_url.startswith("https://") else None,
            candidate.source_url,
        ),
        QCCheck(
            "title_has_report_year",
            "pass" if title_year == candidate.report_year else "fail",
            f"title year={title_year}, expected={candidate.report_year}",
            title_year,
        ),
        QCCheck(
            "title_is_full_report",
            "fail" if any(marker in title for marker in ("摘要", "英文", "取消")) else "pass",
            title,
            title,
        ),
    ]
    return checks


def validate_scholarly_candidate(candidate: Any) -> list[QCCheck]:
    """Validate normalized scholarly metadata before persistence."""

    title = clean_title(candidate.title)
    source_url = str(candidate.source_url or "")
    checks = [
        QCCheck(
            "scholarly_work_uid_present",
            "pass" if getattr(candidate, "work_uid", "") else "fail",
            "work_uid is missing" if not getattr(candidate, "work_uid", "") else None,
            getattr(candidate, "work_uid", None),
        ),
        QCCheck(
            "scholarly_title_present",
            "pass" if title else "fail",
            "title is empty" if not title else None,
            title,
        ),
        QCCheck(
            "scholarly_source_url",
            "pass" if source_url.startswith("https://") else "warn",
            "source URL is not HTTPS" if not source_url.startswith("https://") else None,
            source_url,
        ),
        QCCheck(
            "scholarly_metadata_hash_present",
            "pass" if getattr(candidate, "metadata_hash", "") else "fail",
            "metadata_hash is missing" if not getattr(candidate, "metadata_hash", "") else None,
            getattr(candidate, "metadata_hash", None),
        ),
    ]
    return checks


def validate_financial_metadata(record: Mapping[str, Any]) -> list[QCCheck]:
    """Produce non-blocking quality hints for normalized financial metadata."""

    stock_code = str(record.get("stock_code") or "")
    report_type = str(record.get("report_type") or "")
    report_year = record.get("report_year")
    title = str(record.get("title") or "")
    announcement_date = record.get("announcement_date")
    raw_file_hash = str(record.get("raw_file_hash") or "")
    missing_fields = [
        field_name
        for field_name in REQUIRED_FINANCIAL_METADATA_FIELDS
        if record.get(field_name) in (None, "")
    ]
    try:
        normalized_year = int(report_year)
    except (TypeError, ValueError):
        normalized_year = None
    title_year_match = METADATA_YEAR_PATTERN.search(title)
    title_year = int(title_year_match.group(1)) if title_year_match else None
    date_valid = True
    if announcement_date:
        try:
            date.fromisoformat(str(announcement_date))
        except ValueError:
            date_valid = False

    return [
        QCCheck(
            "metadata_required_fields",
            "pass" if not missing_fields else "warn",
            (
                "missing required metadata: " + ", ".join(missing_fields)
                if missing_fields
                else None
            ),
            {"missing_fields": missing_fields},
        ),
        QCCheck(
            "metadata_stock_code_format",
            "pass" if STOCK_CODE_PATTERN.fullmatch(stock_code) else "warn",
            (
                "stock_code must be a six-digit code"
                if stock_code
                else "stock_code could not be determined"
            ),
            stock_code or None,
        ),
        QCCheck(
            "metadata_report_type",
            "pass" if report_type in SUPPORTED_FINANCIAL_REPORT_TYPES else "warn",
            (
                "unsupported or missing report_type; choose from "
                + ", ".join(sorted(SUPPORTED_FINANCIAL_REPORT_TYPES))
                if report_type not in SUPPORTED_FINANCIAL_REPORT_TYPES
                else None
            ),
            report_type or None,
        ),
        QCCheck(
            "metadata_report_year",
            "pass" if normalized_year is not None and 1990 <= normalized_year <= 2100 else "warn",
            "report_year is missing or out of range"
            if normalized_year is None or not 1990 <= normalized_year <= 2100
            else None,
            normalized_year,
        ),
        QCCheck(
            "metadata_title_year_consistency",
            "pass"
            if normalized_year is not None and (title_year is None or title_year == normalized_year)
            else "warn",
            (
                f"title year={title_year}, metadata year={normalized_year}"
                if title_year is not None and title_year != normalized_year
                else "report year could not be compared with title"
                if normalized_year is None
                else None
            ),
            {"title_year": title_year, "report_year": normalized_year},
        ),
        QCCheck(
            "metadata_announcement_date",
            "pass" if date_valid else "warn",
            "announcement_date must use ISO YYYY-MM-DD" if not date_valid else None,
            announcement_date,
        ),
        QCCheck(
            "metadata_raw_hash",
            "pass" if METADATA_HASH_PATTERN.fullmatch(raw_file_hash) else "warn",
            "raw_file_hash is missing or not a SHA-256 digest"
            if not METADATA_HASH_PATTERN.fullmatch(raw_file_hash)
            else None,
            raw_file_hash or None,
        ),
    ]

def validate_asset(
    record: Mapping[str, Any], data_root: Path | str, *, min_size_bytes: int = 100
) -> list[QCCheck]:
    """Validate a downloaded/reused raw asset and its lineage fields."""

    root = Path(data_root).resolve()
    raw_path_value = record.get("raw_path")
    checks: list[QCCheck] = []
    if not raw_path_value:
        return [QCCheck("asset_path_present", "fail", "raw_path is missing")]

    asset_path = (root / str(raw_path_value)).resolve()
    try:
        asset_path.relative_to(root)
        path_safe = True
    except ValueError:
        path_safe = False
    checks.append(
        QCCheck(
            "asset_path_safe",
            "pass" if path_safe else "fail",
            None if path_safe else "raw_path escapes data_root",
            str(raw_path_value),
        )
    )
    if not path_safe or not asset_path.exists():
        checks.append(
            QCCheck(
                "asset_exists",
                "fail",
                "raw asset does not exist" if path_safe else "asset path is unsafe",
                str(asset_path),
            )
        )
        return checks

    inspection = inspect_pdf(asset_path, min_size_bytes=min_size_bytes)
    checks.append(
        QCCheck(
            "pdf_structure",
            "pass" if inspection.valid else "fail",
            inspection.error,
            {"file_size": inspection.file_size, "page_count": inspection.page_count},
        )
    )
    expected_size = record.get("file_size")
    checks.append(
        QCCheck(
            "file_size_matches",
            "pass" if expected_size in (None, inspection.file_size) else "fail",
            f"expected={expected_size}, actual={inspection.file_size}",
            inspection.file_size,
        )
    )
    expected_hash = record.get("raw_file_hash")
    actual_hash = sha256_file(asset_path)
    checks.append(
        QCCheck(
            "raw_hash_matches",
            "pass" if expected_hash == actual_hash else "fail",
            f"expected={expected_hash}, actual={actual_hash}",
            actual_hash,
        )
    )
    checks.append(
        QCCheck(
            "page_count_available",
            "pass" if inspection.page_count else "warn",
            "page count could not be inferred" if not inspection.page_count else None,
            inspection.page_count,
        )
    )
    return checks


def validate_governed_document(record: Mapping[str, Any]) -> list[QCCheck]:
    """Validate a governed document before it is persisted."""

    blocks = record.get("blocks") or []
    char_count = int(record.get("char_count") or 0)
    return [
        QCCheck(
            "governed_uid_present",
            "pass" if record.get("governed_uid") else "fail",
            "governed_uid is missing" if not record.get("governed_uid") else None,
            record.get("governed_uid"),
        ),
        QCCheck(
            "governed_blocks_nonempty",
            "pass" if blocks else "fail",
            "cleaning produced no blocks" if not blocks else None,
            len(blocks),
        ),
        QCCheck(
            "governed_text_nonempty",
            "pass" if char_count > 0 else "fail",
            "cleaning produced no text" if char_count <= 0 else None,
            char_count,
        ),
        QCCheck(
            "governed_lineage",
            "pass"
            if record.get("work_uid") and record.get("asset_uid") and record.get("input_path")
            else "fail",
            "work_uid, asset_uid or input_path is missing",
            {
                "work_uid": record.get("work_uid"),
                "asset_uid": record.get("asset_uid"),
                "input_path": record.get("input_path"),
            },
        ),
    ]


def validate_governed_chunk(record: Mapping[str, Any]) -> list[QCCheck]:
    """Validate one semantic chunk before it is persisted."""

    char_start = record.get("char_start")
    char_end = record.get("char_end")
    text = record.get("text") or ""
    return [
        QCCheck(
            "chunk_id_present",
            "pass" if record.get("chunk_id") or record.get("chunk_uid") else "fail",
            "chunk_id is missing" if not (record.get("chunk_id") or record.get("chunk_uid")) else None,
            record.get("chunk_id") or record.get("chunk_uid"),
        ),
        QCCheck(
            "chunk_version_uid_present",
            "pass"
            if record.get("chunk_version_uid") or record.get("chunk_uid")
            else "fail",
            "chunk_version_uid is missing"
            if not (record.get("chunk_version_uid") or record.get("chunk_uid"))
            else None,
            record.get("chunk_version_uid") or record.get("chunk_uid"),
        ),
        QCCheck(
            "chunk_uid_present",
            "pass" if record.get("chunk_uid") else "fail",
            "chunk_uid is missing" if not record.get("chunk_uid") else None,
            record.get("chunk_uid"),
        ),
        QCCheck(
            "chunk_text_nonempty",
            "pass" if str(text).strip() else "fail",
            "chunk text is empty" if not str(text).strip() else None,
            len(str(text)),
        ),
        QCCheck(
            "chunk_char_range",
            "pass"
            if isinstance(char_start, int)
            and isinstance(char_end, int)
            and 0 <= char_start <= char_end
            else "fail",
            "invalid char_start/char_end",
            {"char_start": char_start, "char_end": char_end},
        ),
        QCCheck(
            "chunk_page_known",
            "pass" if record.get("page") is not None else "warn",
            "page is unknown; OCR result.json was not available",
            record.get("page"),
        ),
    ]
