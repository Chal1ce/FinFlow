"""Idempotent PDF downloader for report manifests.

The downloader intentionally does not know how a website discovers reports.
An upstream spider or a manually maintained JSONL file only needs to provide
one record per report.  Keeping discovery separate from downloading makes it
possible to replace a source without changing the raw-data contract.

Example::

    python -m spiders.downloader \
        --input examples/reports.example.jsonl \
        --data-root data
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from core.ids import logical_report_uid, raw_asset_uid
from core.logging import add_logging_arguments, configure_logging_from_args, get_logger, log_event
from .http_client import trusted_ssl_context


SUPPORTED_REPORT_TYPES = {"annual", "semiannual", "quarterly", "research"}
STOCK_CODE_PATTERN = re.compile(r"^\d{6}$")
PDF_HEADER = b"%PDF-"
PDF_PAGE_PATTERN = re.compile(rb"/Type\s*/Page(?:\s|/|>)")
LOGGER = get_logger(__name__)


def utc_now() -> str:
    """Return an ISO-8601 timestamp with an explicit UTC offset."""

    return datetime.now(timezone.utc).isoformat()


def stable_uid(*parts: object, length: int = 32) -> str:
    """Create a deterministic identifier from normalized string parts."""

    payload = "|".join(str(part).strip() for part in parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:length]


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Calculate a file's SHA-256 without loading it all into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ReportSpec:
    """The minimum metadata required to download one report."""

    stock_code: str
    report_year: int
    source_url: str
    report_type: str = "annual"
    publish_date: str | None = None
    title: str | None = None
    source_name: str | None = None
    source_id: str | None = None
    candidate_uid: str | None = None
    canonical_uid: str | None = None
    source_priority: int = 100

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ReportSpec":
        required = ("stock_code", "report_year", "source_url")
        missing = [field for field in required if not value.get(field)]
        if missing:
            raise ValueError(f"missing required fields: {', '.join(missing)}")

        stock_code = str(value["stock_code"]).zfill(6)
        if not STOCK_CODE_PATTERN.fullmatch(stock_code):
            raise ValueError(f"invalid stock_code: {stock_code!r}")

        try:
            report_year = int(value["report_year"])
        except (TypeError, ValueError) as exc:
            raise ValueError("report_year must be an integer") from exc
        if not 1990 <= report_year <= datetime.now().year + 1:
            raise ValueError(f"report_year is out of range: {report_year}")

        report_type = str(value.get("report_type", "annual")).strip().lower()
        if report_type not in SUPPORTED_REPORT_TYPES:
            choices = ", ".join(sorted(SUPPORTED_REPORT_TYPES))
            raise ValueError(f"unsupported report_type {report_type!r}; choose from {choices}")

        source_url = str(value["source_url"]).strip()
        if not source_url.startswith(("http://", "https://")):
            raise ValueError("source_url must use http:// or https://")

        try:
            source_priority = int(value.get("source_priority", 100))
        except (TypeError, ValueError) as exc:
            raise ValueError("source_priority must be an integer") from exc

        return cls(
            stock_code=stock_code,
            report_year=report_year,
            source_url=source_url,
            report_type=report_type,
            publish_date=str(value["publish_date"]) if value.get("publish_date") else None,
            title=str(value["title"]) if value.get("title") else None,
            source_name=str(value["source_name"]) if value.get("source_name") else None,
            source_id=str(value["source_id"]) if value.get("source_id") else None,
            candidate_uid=str(value["candidate_uid"]) if value.get("candidate_uid") else None,
            canonical_uid=str(value["canonical_uid"]) if value.get("canonical_uid") else None,
            source_priority=source_priority,
        )


@dataclass(frozen=True)
class PdfInspection:
    valid: bool
    error: str | None
    file_size: int
    page_count: int | None


def inspect_pdf(path: Path, min_size_bytes: int = 100) -> PdfInspection:
    """Perform inexpensive input-quality checks on a downloaded PDF.

    This is deliberately a lightweight check.  Full PDF parsing belongs to the
    later PaddleOCR/parser stage, where malformed PDFs can receive richer
    diagnostics.
    """

    file_size = path.stat().st_size
    if file_size < min_size_bytes:
        return PdfInspection(False, f"file is too small: {file_size} bytes", file_size, None)

    with path.open("rb") as handle:
        header = handle.read(len(PDF_HEADER))
        handle.seek(max(0, file_size - 2048))
        tail = handle.read(2048)

    if header != PDF_HEADER:
        return PdfInspection(False, "file does not start with a PDF header", file_size, None)
    if b"%%EOF" not in tail:
        return PdfInspection(False, "PDF end marker %%EOF was not found", file_size, None)

    page_count = len(PDF_PAGE_PATTERN.findall(path.read_bytes()))
    return PdfInspection(True, None, file_size, page_count or None)


class ManifestStore:
    """Small JSONL-backed index used before PostgreSQL is introduced.

    The file is rewritten atomically after each update.  It therefore contains
    the latest state for each source URL, while pipeline history can later move
    to ``dwd_pipeline_log``.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.records: dict[str, dict[str, Any]] = {}
        self._load()

    @staticmethod
    def request_uid(source_url: str) -> str:
        return stable_uid("request", source_url)

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    key = record.get("request_uid") or self.request_uid(record["source_url"])
                    self.records[key] = record
                except (KeyError, json.JSONDecodeError) as exc:
                    raise ValueError(f"invalid manifest line {line_number}: {exc}") from exc

    def by_url(self, source_url: str) -> dict[str, Any] | None:
        return self.records.get(self.request_uid(source_url))

    def by_hash(self, raw_file_hash: str) -> dict[str, Any] | None:
        for record in self.records.values():
            if record.get("raw_file_hash") == raw_file_hash and record.get("raw_path"):
                return record
        return None

    def upsert(self, record: Mapping[str, Any]) -> None:
        key = str(record.get("request_uid") or self.request_uid(str(record["source_url"])))
        self.records[key] = dict(record)
        self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for record in self.records.values():
                    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        finally:
            temporary_path.unlink(missing_ok=True)


class ReportDownloader:
    """Download reports into an immutable raw-data directory."""

    def __init__(
        self,
        data_root: Path | str = Path("data"),
        timeout_seconds: float = 30,
        user_agent: str = "fin-doc-governance/0.1",
        min_size_bytes: int = 100,
    ) -> None:
        self.data_root = Path(data_root)
        self.raw_root = self.data_root / "raw_pdfs"
        self.quarantine_root = self.data_root / "quarantine"
        self.temp_root = self.data_root / ".tmp"
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.min_size_bytes = min_size_bytes
        self.manifest = ManifestStore(self.data_root / "manifests" / "reports.jsonl")

    def download(self, spec: ReportSpec, *, refresh: bool = False) -> dict[str, Any]:
        """Download or reuse one report and return its manifest record."""

        log_event(
            LOGGER,
            "INFO",
            "report_download_started",
            source_name=spec.source_name,
            stock_code=spec.stock_code,
            report_year=spec.report_year,
            report_type=spec.report_type,
            source_url=spec.source_url,
        )
        existing = self.manifest.by_url(spec.source_url)
        if not refresh and existing and existing.get("status") in {"success", "duplicate"}:
            raw_path = existing.get("raw_path")
            if raw_path and (self.data_root / raw_path).exists():
                return self._log_result({**existing, "status": "skipped"})

        self.temp_root.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            temporary_path, raw_file_hash = self._download_to_temp(spec.source_url)
            inspection = inspect_pdf(temporary_path, self.min_size_bytes)
            base_record = self._base_record(spec, raw_file_hash)

            if not inspection.valid:
                quarantine_path = self._quarantine(temporary_path, spec, raw_file_hash)
                record = {
                    **base_record,
                    "status": "failed",
                    "error_msg": inspection.error,
                    "file_size": inspection.file_size,
                    "page_count": inspection.page_count,
                    "quarantine_path": self._relative_path(quarantine_path),
                    "updated_at": utc_now(),
                }
                temporary_path = None
                self.manifest.upsert(record)
                return self._log_result(record)

            duplicate = self.manifest.by_hash(raw_file_hash)
            if duplicate and duplicate.get("raw_path"):
                duplicate_path = self.data_root / str(duplicate["raw_path"])
                if duplicate_path.exists():
                    temporary_path.unlink(missing_ok=True)
                    temporary_path = None
                    record = {
                        **base_record,
                        "status": "duplicate",
                        "report_uid": duplicate.get("report_uid"),
                        "raw_path": duplicate["raw_path"],
                        "file_size": inspection.file_size,
                        "page_count": inspection.page_count,
                        "duplicate_of": duplicate.get("request_uid"),
                        "updated_at": utc_now(),
                    }
                    self.manifest.upsert(record)
                    return self._log_result(record)

            target_path = self._raw_path(spec, raw_file_hash)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary_path, target_path)
            temporary_path = None
            record = {
                **base_record,
                "status": "success",
                "raw_path": self._relative_path(target_path),
                "file_size": inspection.file_size,
                "page_count": inspection.page_count,
                "updated_at": utc_now(),
            }
            self.manifest.upsert(record)
            return self._log_result(record)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            record = {
                **self._base_record(spec, None),
                "status": "failed",
                "error_msg": f"{type(exc).__name__}: {exc}",
                "updated_at": utc_now(),
            }
            self.manifest.upsert(record)
            return self._log_result(record)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def associate_batch(self, record: Mapping[str, Any], batch_id: str) -> dict[str, Any]:
        """Associate a downloaded report asset with a reusable collection batch.

        The downloader's manifest is the durable bridge between web collection
        and the local financial workflow.  A file may be reused by multiple
        batches, so the association is additive and never replaces prior
        lineage.
        """

        if not batch_id:
            raise ValueError("batch_id is required when associating a report")
        source_url = str(record.get("source_url") or "").strip()
        request_uid = str(record.get("request_uid") or "").strip()
        if not request_uid:
            if not source_url:
                raise ValueError("report record requires request_uid or source_url")
            request_uid = ManifestStore.request_uid(source_url)
        existing = self.manifest.records.get(request_uid, {})
        batch_ids = {
            str(item)
            for item in existing.get("batch_ids") or ()
            if str(item).strip()
        }
        if existing.get("batch_id"):
            batch_ids.add(str(existing["batch_id"]))
        batch_ids.add(str(batch_id))
        updated = {
            **existing,
            **dict(record),
            "request_uid": request_uid,
            # A repeated URL download returns the transient status "skipped".
            # Keep the durable result as a successfully materialized asset.
            "status": existing.get("status") or record.get("status"),
            "batch_id": str(batch_id),
            "batch_ids": sorted(batch_ids),
            "batch_associated_at": utc_now(),
        }
        self.manifest.upsert(updated)
        result = {**dict(record), "batch_id": str(batch_id), "batch_ids": sorted(batch_ids)}
        log_event(
            LOGGER,
            "INFO",
            "report_batch_associated",
            batch_id=str(batch_id),
            report_uid=result.get("report_uid"),
            asset_uid=result.get("asset_uid"),
            request_uid=result.get("request_uid"),
            status=result.get("status"),
        )
        return result

    @staticmethod
    def _log_result(record: dict[str, Any]) -> dict[str, Any]:
        log_event(
            LOGGER,
            "ERROR" if record.get("status") == "failed" else "INFO",
            "report_download_finished",
            status=record.get("status"),
            report_uid=record.get("report_uid"),
            asset_uid=record.get("asset_uid"),
            request_uid=record.get("request_uid"),
            source_url=record.get("source_url"),
            raw_path=record.get("raw_path"),
            file_size=record.get("file_size"),
            page_count=record.get("page_count"),
            error_msg=record.get("error_msg"),
        )
        return record

    def _download_to_temp(self, source_url: str) -> tuple[Path, str]:
        request = Request(source_url, headers={"User-Agent": self.user_agent})
        temporary_path: Path | None = None
        try:
            with urlopen(request, timeout=self.timeout_seconds, context=trusted_ssl_context()) as response:
                fd, temporary_name = tempfile.mkstemp(
                    prefix=".report-", suffix=".part", dir=self.temp_root
                )
                temporary_path = Path(temporary_name)
                digest = hashlib.sha256()
                with os.fdopen(fd, "wb") as handle:
                    while chunk := response.read(1024 * 1024):
                        handle.write(chunk)
                        digest.update(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                return temporary_path, digest.hexdigest()
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

    def _base_record(self, spec: ReportSpec, raw_file_hash: str | None) -> dict[str, Any]:
        record = {
            "request_uid": ManifestStore.request_uid(spec.source_url),
            "stock_code": spec.stock_code,
            "report_year": spec.report_year,
            "report_type": spec.report_type,
            "publish_date": spec.publish_date,
            "title": spec.title,
            "source_url": spec.source_url,
            "raw_file_hash": raw_file_hash,
            "source_name": spec.source_name,
            "source_id": spec.source_id,
            "candidate_uid": spec.candidate_uid,
            "canonical_uid": spec.canonical_uid,
            "source_priority": spec.source_priority,
        }
        if raw_file_hash:
            record["report_uid"] = spec.canonical_uid or logical_report_uid(
                spec.stock_code, spec.report_year, spec.report_type
            )
            record["document_uid"] = record["report_uid"]
            record["asset_uid"] = raw_asset_uid(raw_file_hash)
        return record

    def _raw_path(self, spec: ReportSpec, raw_file_hash: str) -> Path:
        filename = (
            f"{spec.stock_code}_{spec.report_year}_{spec.report_type}_"
            f"{raw_file_hash[:12]}.pdf"
        )
        return self.raw_root / str(spec.report_year) / spec.stock_code / filename

    def _quarantine(self, temporary_path: Path, spec: ReportSpec, raw_file_hash: str) -> Path:
        target = (
            self.quarantine_root
            / str(spec.report_year)
            / spec.stock_code
            / f"{spec.stock_code}_{spec.report_year}_{raw_file_hash[:12]}.pdf"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary_path, target)
        return target

    def _relative_path(self, path: Path) -> str:
        return path.relative_to(self.data_root).as_posix()


def load_specs(path: Path | str) -> Iterable[ReportSpec]:
    """Load and validate report specifications from JSONL."""

    input_path = Path(path)
    with input_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("each line must be a JSON object")
                yield ReportSpec.from_mapping(value)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"invalid input line {line_number}: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download financial report PDFs from a JSONL manifest")
    parser.add_argument("--input", required=True, type=Path, help="input report list in JSONL format")
    parser.add_argument("--data-root", type=Path, default=Path("data"), help="local data lake root")
    parser.add_argument("--timeout", type=float, default=30, help="HTTP timeout in seconds")
    parser.add_argument("--min-size", type=int, default=100, help="minimum accepted PDF size")
    parser.add_argument("--user-agent", default="fin-doc-governance/0.1", help="HTTP User-Agent")
    add_logging_arguments(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        configure_logging_from_args(args)
    except (OSError, ValueError) as exc:
        print(f"error: {type(exc).__name__}: {exc}")
        return 2
    log_event(
        LOGGER,
        "INFO",
        "command_started",
        command="spiders.downloader",
        input_path=str(args.input),
        data_root=str(args.data_root),
    )
    downloader = ReportDownloader(
        data_root=args.data_root,
        timeout_seconds=args.timeout,
        min_size_bytes=args.min_size,
        user_agent=args.user_agent,
    )
    failed = 0
    try:
        for spec in load_specs(args.input):
            result = downloader.download(spec)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            failed += result.get("status") == "failed"
    except (OSError, ValueError) as exc:
        log_event(
            LOGGER,
            "ERROR",
            "command_failed",
            command="spiders.downloader",
            error_msg=f"{type(exc).__name__}: {exc}",
        )
        print(f"error: {exc}")
        return 2
    log_event(
        LOGGER,
        "INFO" if not failed else "ERROR",
        "command_finished",
        command="spiders.downloader",
        failed=failed,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
