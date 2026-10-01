"""Download openly accessible scholarly PDFs with immutable raw assets."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

from core.ids import raw_asset_uid
from .downloader import inspect_pdf, sha256_file, utc_now
from .http_client import HttpClient, write_jsonl_atomic


BLOCKED_HTTP_CODES = {401, 403, 407}
RETRYABLE_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class ScholarlyPdfDownloader:
    """Download OA PDFs idempotently and keep a separate scholarly manifest."""

    def __init__(
        self,
        data_root: Path | str = Path("data"),
        *,
        http_client: HttpClient,
        min_size_bytes: int = 100,
        download_attempts: int = 3,
        download_backoff_seconds: float = 2.0,
        browser_user_agent: str | None = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.http = http_client
        self.min_size_bytes = min_size_bytes
        self.download_attempts = max(1, download_attempts)
        self.download_backoff_seconds = max(0.0, download_backoff_seconds)
        self.browser_user_agent = browser_user_agent
        self.raw_root = self.data_root / "raw_pdfs" / "scholarly"
        self.named_root = self.data_root / "named_pdfs" / "scholarly"
        self.quarantine_root = self.data_root / "quarantine" / "scholarly"
        self.temp_root = self.data_root / ".tmp" / "scholarly"
        self.manifest_path = self.data_root / "manifests" / "scholarly_documents.jsonl"
        self.records: dict[str, dict[str, Any]] = {}
        self._load_manifest()

    def download(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        """Download one candidate, or return metadata_only when no OA PDF exists."""

        work_uid = str(candidate.get("work_uid") or "")
        candidate_uid = str(candidate.get("candidate_uid") or "")
        pdf_url = str(candidate.get("pdf_url") or "").strip()
        if not work_uid or not candidate_uid:
            raise ValueError("scholarly candidate requires work_uid and candidate_uid")
        base = {
            # ``work_uid`` is the logical document identity.  The concrete
            # file version is represented separately by ``asset_uid``.
            "document_uid": work_uid,
            "work_uid": work_uid,
            "candidate_uid": candidate_uid,
            "source_name": candidate.get("source_name"),
            "source_id": candidate.get("source_id"),
            "source_url": pdf_url,
            "landing_url": candidate.get("source_url"),
            "pdf_url": pdf_url or None,
            "title": candidate.get("title"),
            "published_date": candidate.get("published_date"),
            "source_updated_at": candidate.get("source_updated_at"),
            "updated_at": utc_now(),
        }
        if not pdf_url or not bool(candidate.get("open_access")):
            return {
                **base,
                "status": "metadata_only",
                "error_msg": "no explicitly open-access PDF URL",
            }
        parsed = urlparse(pdf_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return {
                **base,
                "status": "failed",
                "failure_type": "invalid",
                "error_msg": "pdf_url is not a valid HTTP URL",
            }

        existing = self.records.get(candidate_uid)
        if (
            existing
            and existing.get("status") == "blocked"
            and existing.get("pdf_url") == pdf_url
            and existing.get("source_updated_at") == candidate.get("source_updated_at")
        ):
            return {
                **existing,
                "title": candidate.get("title"),
                "status": "blocked",
                "skipped": True,
                "updated_at": utc_now(),
            }
        if (
            existing
            and existing.get("status") in {"success", "duplicate"}
            and existing.get("pdf_url") == pdf_url
            and existing.get("source_updated_at") == candidate.get("source_updated_at")
        ):
            raw_path = existing.get("raw_path")
            if raw_path and (self.data_root / str(raw_path)).exists():
                record = {
                    **existing,
                    "title": candidate.get("title"),
                    "status": "skipped",
                    "updated_at": utc_now(),
                }
                self._attach_named_copy(record)
                return record

        temporary_path: Path | None = None
        try:
            self.temp_root.mkdir(parents=True, exist_ok=True)
            temporary_path = self._download_with_fallback(
                pdf_url, referer=str(base.get("landing_url") or "")
            )
            inspection = inspect_pdf(temporary_path, min_size_bytes=self.min_size_bytes)
            file_hash = sha256_file(temporary_path)
            if not inspection.valid:
                quarantine_path = self._quarantine(
                    temporary_path, work_uid, file_hash
                )
                temporary_path = None
                record = {
                    **base,
                    "status": "failed",
                    "failure_type": "invalid",
                    "error_msg": inspection.error,
                    "raw_file_hash": file_hash,
                    "file_size": inspection.file_size,
                    "page_count": inspection.page_count,
                    "quarantine_path": self._relative_path(quarantine_path),
                }
                self._upsert_manifest(record)
                return record

            duplicate = self._by_hash(file_hash)
            if duplicate and duplicate.get("raw_path"):
                duplicate_path = self.data_root / str(duplicate["raw_path"])
                if duplicate_path.exists():
                    temporary_path.unlink(missing_ok=True)
                    temporary_path = None
                    record = {
                        **base,
                        "status": "duplicate",
                        "raw_file_hash": file_hash,
                        "raw_path": duplicate["raw_path"],
                        "asset_uid": raw_asset_uid(file_hash),
                        "file_size": inspection.file_size,
                        "page_count": inspection.page_count,
                    }
                    self._attach_named_copy(record)
                    self._upsert_manifest(record)
                    return record

            raw_path = self._raw_path(work_uid, candidate.get("published_date"), file_hash)
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary_path, raw_path)
            temporary_path = None
            record = {
                **base,
                "status": "success",
                "raw_file_hash": file_hash,
                "raw_path": self._relative_path(raw_path),
                "asset_uid": raw_asset_uid(file_hash),
                "file_size": inspection.file_size,
                "page_count": inspection.page_count,
            }
            self._attach_named_copy(record)
            self._upsert_manifest(record)
            return record
        except HTTPError as exc:
            record = self._failure_record(base, exc)
            if record["failure_type"] == "blocked":
                self._upsert_manifest(record)
            return record
        except (URLError, TimeoutError, OSError) as exc:
            return self._failure_record(base, exc)
        finally:
            if temporary_path:
                temporary_path.unlink(missing_ok=True)

    def _download_to_temp(self, url: str, *, headers: Mapping[str, str]) -> Path:
        content = self.http.request_bytes(url, headers=headers)
        fd, temporary_name = tempfile.mkstemp(
            prefix=".scholarly-", suffix=".download", dir=self.temp_root
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            return temporary_path
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

    def _download_with_fallback(self, url: str, *, referer: str) -> Path:
        """Download with transient retries and one browser-like 403 retry."""

        last_error: BaseException | None = None
        browser_retry_used = False
        for attempt in range(self.download_attempts):
            try:
                return self._download_to_temp(
                    url, headers={"Accept": "application/pdf"}
                )
            except HTTPError as exc:
                last_error = exc
                if exc.code in BLOCKED_HTTP_CODES and not browser_retry_used:
                    browser_retry_used = True
                    fallback_headers = {
                        "Accept": "application/pdf,text/html,*/*;q=0.8",
                        "Referer": referer,
                        "User-Agent": self.browser_user_agent or BROWSER_USER_AGENT,
                    }
                    try:
                        return self._download_to_temp(url, headers=fallback_headers)
                    except (HTTPError, URLError, TimeoutError, OSError) as fallback_exc:
                        last_error = fallback_exc
                        if (
                            isinstance(fallback_exc, HTTPError)
                            and fallback_exc.code not in RETRYABLE_HTTP_CODES
                        ):
                            break
                elif exc.code not in RETRYABLE_HTTP_CODES:
                    break
            except (URLError, TimeoutError, OSError) as exc:
                last_error = exc
            if attempt + 1 < self.download_attempts:
                delay = min(self.download_backoff_seconds * (2**attempt), 60.0)
                if delay > 0:
                    time.sleep(delay)
        if last_error is None:
            raise OSError(f"download failed after {self.download_attempts} attempts")
        raise last_error

    def _failure_record(
        self, base: Mapping[str, Any], exc: BaseException
    ) -> dict[str, Any]:
        status_code = getattr(exc, "code", None)
        if isinstance(exc, HTTPError) and status_code in BLOCKED_HTTP_CODES:
            failure_type = "blocked"
        elif isinstance(exc, HTTPError) and status_code not in RETRYABLE_HTTP_CODES:
            failure_type = "not_found" if status_code == 404 else "permanent"
        else:
            failure_type = "retryable"
        record = {
            **base,
            "status": "blocked" if failure_type == "blocked" else "failed",
            "failure_type": failure_type,
            "error_msg": f"{type(exc).__name__}: {exc}",
        }
        if status_code is not None:
            record["http_status"] = int(status_code)
        return record

    def _raw_path(self, work_uid: str, published_date: object, file_hash: str) -> Path:
        year = _year_from_date(published_date)
        return self.raw_root / year / work_uid / f"{work_uid}_{file_hash[:12]}.pdf"

    def _attach_named_copy(self, record: dict[str, Any]) -> None:
        named_path = self._materialize_named_copy(record)
        if named_path:
            record["named_path"] = self._relative_path(named_path)

    def _materialize_named_copy(self, record: dict[str, Any]) -> Path | None:
        raw_relative = record.get("raw_path")
        if not raw_relative:
            return None
        source = self.data_root / str(raw_relative)
        if not source.exists():
            return None
        year = _year_from_date(record.get("published_date"))
        file_hash = str(record.get("raw_file_hash") or "")
        if not file_hash:
            return None

        existing = self._by_hash(file_hash)
        if existing and existing.get("named_path"):
            existing_path = self.data_root / str(existing["named_path"])
            if existing_path.exists():
                if isinstance(existing.get("named_number"), int):
                    record["named_number"] = existing["named_number"]
                return existing_path

        number = self._next_named_number()
        filename = f"{number}.pdf"
        target = self.named_root / year / filename
        if target.exists() and target.stat().st_size == source.stat().st_size:
            record["named_number"] = number
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        record["named_number"] = number
        return target

    def _next_named_number(self) -> int:
        numbers = [
            int(record["named_number"])
            for record in self.records.values()
            if isinstance(record.get("named_number"), int)
        ]
        return max(numbers, default=-1) + 1

    def _quarantine(self, temporary_path: Path, work_uid: str, file_hash: str) -> Path:
        target = self.quarantine_root / work_uid / f"{work_uid}_{file_hash[:12]}.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary_path, target)
        return target

    def _relative_path(self, path: Path) -> str:
        return path.relative_to(self.data_root).as_posix()

    def _load_manifest(self) -> None:
        if not self.manifest_path.exists():
            return
        with self.manifest_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    key = str(record["candidate_uid"])
                    self.records[key] = record
                except (KeyError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        f"invalid scholarly manifest line {line_number}: {exc}"
                    ) from exc

    def _by_hash(self, file_hash: str) -> dict[str, Any] | None:
        for record in self.records.values():
            if record.get("raw_file_hash") == file_hash and record.get("raw_path"):
                return record
        return None

    def _upsert_manifest(self, record: Mapping[str, Any]) -> None:
        self.records[str(record["candidate_uid"])] = dict(record)
        write_jsonl_atomic(self.manifest_path, list(self.records.values()))


def _year_from_date(value: object) -> str:
    year = str(value or "unknown")[:4]
    return year if year.isdigit() else "unknown"
