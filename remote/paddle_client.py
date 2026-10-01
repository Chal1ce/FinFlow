"""Client for a remote PaddleX/PP-StructureV3 serving endpoint.

The current PaddleOCR 3.x basic serving endpoint is ``POST /layout-parsing``
and returns binary results as Base64 by default.  This client also accepts URL
values so it remains compatible with hosted or older deployments.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import tempfile
from http.client import IncompleteRead
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import load_config
from core.logging import add_logging_arguments, configure_logging_from_args, get_logger, log_event


DATA_URI_PATTERN = re.compile(r"^data:[^;]+;base64,(.*)$", re.DOTALL)
SEGMENT_MANIFEST_SCHEMA_VERSION = "paddle-ocr-segments-v1"
LOGGER = get_logger(__name__)


class PaddleServingError(RuntimeError):
    """Raised when the remote Paddle serving request or response fails."""


def safe_relative_path(value: str, fallback: str = "asset.bin") -> Path:
    """Prevent response-provided image paths from escaping the output folder."""

    normalized = str(value).replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        return Path(fallback)
    clean_parts = [part for part in path.parts if part not in ("", ".")]
    return Path(*clean_parts) if clean_parts else Path(fallback)


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    """Write OCR lineage JSON without exposing a partially written manifest."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(value), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


class PaddleOCRClient:
    """Upload PDFs and materialize PP-StructureV3 results locally."""

    def __init__(
        self,
        api_url: str,
        *,
        token: str | None = None,
        timeout_seconds: float | None = None,
        user_agent: str | None = None,
        page_batch_size: int | None = None,
    ) -> None:
        configured = load_config().local_paddle
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.timeout_seconds = (
            configured.timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        self.user_agent = user_agent or configured.user_agent
        self.page_batch_size = (
            configured.page_batch_size if page_batch_size is None else page_batch_size
        )
        if self.page_batch_size < 1:
            raise ValueError("page_batch_size must be at least one")
        self.default_options = dict(configured.optional_payload)

    def process_pdf(
        self,
        pdf_path: Path | str,
        output_dir: Path | str,
        *,
        options: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        source_path = Path(pdf_path)
        if not source_path.exists():
            raise FileNotFoundError(source_path)
        if source_path.suffix.lower() != ".pdf":
            raise ValueError(f"input is not a PDF: {source_path}")

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        page_count = self._page_count(source_path)
        effective_options = {**self.default_options, **dict(options or {})}
        manifest = self._load_or_create_segment_manifest(
            source_path, output_path, page_count, effective_options
        )
        failures: list[str] = []
        with tempfile.TemporaryDirectory(prefix="paddle-ocr-pages-") as temporary_name:
            temporary_dir = Path(temporary_name)
            for segment in manifest["segments"]:
                if self._segment_is_reusable(output_path, segment):
                    continue
                try:
                    segment_pdf = self._segment_pdf_path(
                        source_path, segment, page_count, temporary_dir
                    )
                    result = self._request_segment(segment_pdf, effective_options)
                    pages = result.get("layoutParsingResults")
                    expected_pages = int(segment["end_page"]) - int(segment["start_page"]) + 1
                    if not isinstance(pages, list) or len(pages) != expected_pages:
                        actual_pages = len(pages) if isinstance(pages, list) else "non-list"
                        raise PaddleServingError(
                            f"Serving returned {actual_pages} pages for pages "
                            f"{segment['start_page']}-{segment['end_page']}; expected {expected_pages}"
                        )
                    result_path = output_path / str(segment["result_path"])
                    _write_json_atomic(result_path, result)
                    segment.update(
                        {
                            "status": "success",
                            "page_result_count": len(pages),
                            "error": None,
                        }
                    )
                except (OSError, ValueError, PaddleServingError) as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    segment.update(
                        {
                            "status": "failed",
                            "page_result_count": None,
                            "error": message,
                        }
                    )
                    failures.append(
                        f"{segment['segment_uid']} pages {segment['start_page']}-{segment['end_page']}: {message}"
                    )
                self._write_segment_manifest(output_path, manifest)

        if failures:
            self._clear_incomplete_document_output(output_path)
            raise PaddleServingError(
                f"{len(failures)} OCR segment(s) failed; successful segments are retained for retry: "
                + "; ".join(failures)
            )

        try:
            saved_image_count = self._materialize_complete_document(output_path, manifest)
        except (OSError, ValueError, PaddleServingError) as exc:
            self._clear_incomplete_document_output(output_path)
            raise PaddleServingError(
                f"cannot materialize complete OCR output from successful segments: {exc}"
            ) from exc
        return {
            "source_path": str(source_path),
            "output_dir": str(output_path),
            "markdown_path": str(output_path / "output.md"),
            "page_count": page_count,
            "page_result_count": page_count,
            "page_batch_size": self.page_batch_size,
            "segment_count": len(manifest["segments"]),
            "segment_manifest_path": str(output_path / "ocr_segments.json"),
            "saved_image_count": saved_image_count,
        }

    def _page_count(self, source_path: Path) -> int:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise PaddleServingError(
                "pypdf is required for page-aware OCR; install project dependencies first"
            ) from exc
        try:
            reader = PdfReader(source_path)
            page_count = len(reader.pages)
        except Exception as exc:
            raise PaddleServingError(f"cannot read PDF page count: {source_path}") from exc
        if page_count < 1:
            raise PaddleServingError("PDF contains no pages")
        return page_count

    def _load_or_create_segment_manifest(
        self,
        source_path: Path,
        output_path: Path,
        page_count: int,
        effective_options: Mapping[str, Any],
    ) -> dict[str, Any]:
        manifest_path = output_path / "ocr_segments.json"
        source_hash = _sha256_file(source_path)
        expected_identity = {
            "schema_version": SEGMENT_MANIFEST_SCHEMA_VERSION,
            "source_sha256": source_hash,
            "page_count": page_count,
            "page_batch_size": self.page_batch_size,
            "request_options": dict(effective_options),
        }
        existing: Mapping[str, Any] | None = None
        if manifest_path.is_file():
            try:
                parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                parsed = None
            if isinstance(parsed, Mapping):
                existing = parsed
        if existing and all(existing.get(key) == value for key, value in expected_identity.items()):
            segments = existing.get("segments")
            if isinstance(segments, list) and self._segments_match_expected(
                segments, page_count
            ):
                return dict(existing)

        segments: list[dict[str, Any]] = []
        for segment_index, start_page in enumerate(range(1, page_count + 1, self.page_batch_size)):
            end_page = min(page_count, start_page + self.page_batch_size - 1)
            segment_uid = f"segment-{segment_index + 1:04d}"
            segments.append(
                {
                    "segment_uid": segment_uid,
                    "segment_index": segment_index,
                    "start_page": start_page,
                    "end_page": end_page,
                    "status": "pending",
                    "page_result_count": None,
                    "error": None,
                    "result_path": f"segments/{segment_uid}/result.json",
                }
            )
        manifest = {
            **expected_identity,
            "source_path": str(source_path),
            "segments": segments,
        }
        self._write_segment_manifest(output_path, manifest)
        return manifest

    def _segment_count(self, page_count: int) -> int:
        return (page_count + self.page_batch_size - 1) // self.page_batch_size

    def _segments_match_expected(
        self, segments: list[Any], page_count: int
    ) -> bool:
        """Reject malformed manifests so page coverage cannot be guessed."""

        expected_starts = range(1, page_count + 1, self.page_batch_size)
        if len(segments) != self._segment_count(page_count):
            return False
        for segment_index, start_page in enumerate(expected_starts):
            segment = segments[segment_index]
            if not isinstance(segment, Mapping):
                return False
            end_page = min(page_count, start_page + self.page_batch_size - 1)
            segment_uid = f"segment-{segment_index + 1:04d}"
            if (
                segment.get("segment_uid") != segment_uid
                or segment.get("segment_index") != segment_index
                or segment.get("start_page") != start_page
                or segment.get("end_page") != end_page
                or segment.get("result_path") != f"segments/{segment_uid}/result.json"
            ):
                return False
        return True

    @staticmethod
    def _write_segment_manifest(output_path: Path, manifest: Mapping[str, Any]) -> None:
        _write_json_atomic(output_path / "ocr_segments.json", manifest)

    def _segment_is_reusable(
        self, output_path: Path, segment: Mapping[str, Any]
    ) -> bool:
        if segment.get("status") != "success":
            return False
        expected_pages = int(segment["end_page"]) - int(segment["start_page"]) + 1
        result_path = output_path / str(segment.get("result_path") or "")
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        pages = result.get("layoutParsingResults") if isinstance(result, Mapping) else None
        return isinstance(pages, list) and len(pages) == expected_pages

    def _segment_pdf_path(
        self,
        source_path: Path,
        segment: Mapping[str, Any],
        page_count: int,
        temporary_dir: Path,
    ) -> Path:
        if int(segment["start_page"]) == 1 and int(segment["end_page"]) == page_count:
            return source_path
        try:
            from pypdf import PdfReader, PdfWriter
        except ImportError as exc:
            raise PaddleServingError(
                "pypdf is required for page-aware OCR; install project dependencies first"
            ) from exc
        output = temporary_dir / f"{segment['segment_uid']}.pdf"
        try:
            reader = PdfReader(source_path)
            writer = PdfWriter()
            for page_index in range(int(segment["start_page"]) - 1, int(segment["end_page"])):
                writer.add_page(reader.pages[page_index])
            with output.open("wb") as handle:
                writer.write(handle)
        except Exception as exc:
            raise PaddleServingError(
                f"cannot create PDF pages {segment['start_page']}-{segment['end_page']}"
            ) from exc
        return output

    def _request_segment(
        self, pdf_path: Path, effective_options: Mapping[str, Any]
    ) -> dict[str, Any]:
        file_data = base64.b64encode(pdf_path.read_bytes()).decode("ascii")
        response = self._post_json(
            {"file": file_data, "fileType": 0, **dict(effective_options)}
        )
        result = response.get("result")
        if not isinstance(result, dict):
            raise PaddleServingError(
                response.get("errorMsg") or "serving response does not contain result"
            )
        return result

    def _materialize_complete_document(
        self, output_path: Path, manifest: Mapping[str, Any]
    ) -> int:
        """Build compact, ordered final outputs without retaining all pages in RAM.

        ``segments/*/result.json`` keeps the complete Serving responses, including
        Base64 image fields.  The final ``result.json`` is a compact assembled
        representation used by governance; it excludes binary response fields
        already materialized under ``images/``.
        """

        descriptor, result_name = tempfile.mkstemp(
            prefix=".result.json.", suffix=".tmp", dir=output_path
        )
        markdown_descriptor, markdown_name = tempfile.mkstemp(
            prefix=".output.md.", suffix=".tmp", dir=output_path
        )
        result_temporary_path = Path(result_name)
        markdown_temporary_path = Path(markdown_name)
        saved_image_count = 0
        seen_pages = 0
        try:
            with (
                os.fdopen(descriptor, "w", encoding="utf-8") as result_handle,
                os.fdopen(markdown_descriptor, "w", encoding="utf-8") as markdown_handle,
            ):
                result_handle.write('{"layoutParsingResults":[')
                first_page = True
                first_markdown = True
                for segment in manifest["segments"]:
                    result_path = output_path / str(segment["result_path"])
                    raw_result = json.loads(result_path.read_text(encoding="utf-8"))
                    pages = raw_result.get("layoutParsingResults")
                    expected_pages = int(segment["end_page"]) - int(segment["start_page"]) + 1
                    if not isinstance(pages, list) or len(pages) != expected_pages:
                        raise PaddleServingError(
                            f"saved {segment['segment_uid']} no longer covers its expected pages"
                        )
                    for local_page_index, page in enumerate(pages):
                        if not isinstance(page, Mapping):
                            raise PaddleServingError(
                                f"saved {segment['segment_uid']} contains a non-object page"
                            )
                        global_page_index = int(segment["start_page"]) - 1 + local_page_index
                        saved_image_count += self._save_page_assets(
                            page, output_path, global_page_index
                        )
                        markdown = page.get("markdown")
                        if isinstance(markdown, Mapping) and markdown.get("text"):
                            if not first_markdown:
                                markdown_handle.write("\n\n")
                            markdown_handle.write(str(markdown["text"]))
                            first_markdown = False
                        if not first_page:
                            result_handle.write(",")
                        compact_page = self._compact_page(page)
                        compact_page["image_assets"] = self._image_asset_mapping(page, output_path, global_page_index)
                        json.dump(
                            compact_page,
                            result_handle,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        first_page = False
                        seen_pages += 1
                result_handle.write(
                    '],"ocr_segment_manifest":"ocr_segments.json",'
                    f'"page_count":{seen_pages}'
                    "}"
                )
                result_handle.flush()
                os.fsync(result_handle.fileno())
                markdown_handle.flush()
                os.fsync(markdown_handle.fileno())
            expected_total = int(manifest["page_count"])
            if seen_pages != expected_total:
                raise PaddleServingError(
                    f"assembled {seen_pages} pages but expected {expected_total}"
                )
            os.replace(result_temporary_path, output_path / "result.json")
            os.replace(markdown_temporary_path, output_path / "output.md")
        finally:
            result_temporary_path.unlink(missing_ok=True)
            markdown_temporary_path.unlink(missing_ok=True)
        return saved_image_count

    @staticmethod
    def _image_asset_mapping(page: Mapping[str, Any], output_path: Path, page_index: int) -> list[dict[str, Any]]:
        assets = []
        if page.get("inputImage"):
            path = output_path / "images" / "input" / f"page_{page_index+1:04d}.jpg"
            assets.append({"source_key": "inputImage", "path": path.relative_to(output_path).as_posix(),
                           "page": page_index+1, "purpose": "original_page", "sha256": _sha256_file(path)})
        for name in ((page.get("markdown") or {}).get("images") or {}):
            path = (output_path / "images" / "markdown" / f"page_{page_index + 1:04d}"
                    / safe_relative_path(str(name), f"image_{len(assets)+1}.bin"))
            assets.append({"source_key": str(name), "path": path.relative_to(output_path).as_posix(),
                           "page": page_index + 1, "purpose": "region", "sha256": _sha256_file(path)})
        for name in (page.get("outputImages") or {}):
            safe_name = safe_relative_path(str(name), f"page_{page_index+1}.jpg").name
            path = output_path / "images" / "output" / f"page_{page_index + 1:04d}_{safe_name}"
            assets.append({"source_key": str(name), "path": path.relative_to(output_path).as_posix(),
                           "page": page_index + 1, "purpose": "page_or_debug", "sha256": _sha256_file(path)})
        return assets

    @staticmethod
    def _compact_page(page: Mapping[str, Any]) -> dict[str, Any]:
        compact = dict(page)
        compact.pop("outputImages", None)
        compact.pop("inputImage", None)
        markdown = compact.get("markdown")
        if isinstance(markdown, Mapping):
            compact["markdown"] = {
                key: value for key, value in markdown.items() if key != "images"
            }
        return compact

    def _save_page_assets(
        self, page: Mapping[str, Any], output_path: Path, page_index: int
    ) -> int:
        saved_image_count = 0
        if page.get("inputImage"):
            self._save_binary(page["inputImage"], output_path / "images" / "input" / f"page_{page_index+1:04d}.jpg")
            saved_image_count += 1
        markdown = page.get("markdown") or {}
        if isinstance(markdown, Mapping):
            images = markdown.get("images") or {}
            if isinstance(images, Mapping):
                for image_name, image_value in images.items():
                    image_path = (
                        output_path
                        / "images"
                        / "markdown"
                        / f"page_{page_index + 1:04d}"
                        / safe_relative_path(
                            str(image_name), f"image_{saved_image_count + 1}.bin"
                        )
                    )
                    self._save_binary(image_value, image_path)
                    saved_image_count += 1
        output_images = page.get("outputImages") or {}
        if isinstance(output_images, Mapping):
            for image_name, image_value in output_images.items():
                safe_name = safe_relative_path(
                    str(image_name), f"page_{page_index + 1}.jpg"
                ).name
                image_path = (
                    output_path
                    / "images"
                    / "output"
                    / f"page_{page_index + 1:04d}_{safe_name}"
                )
                self._save_binary(image_value, image_path)
                saved_image_count += 1
        return saved_image_count

    @staticmethod
    def _clear_incomplete_document_output(output_path: Path) -> None:
        (output_path / "result.json").unlink(missing_ok=True)
        (output_path / "output.md").unlink(missing_ok=True)

    def _post_json(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        request = Request(
            self.api_url,
            data=json.dumps(dict(payload), ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = self._read_response(response, "serving")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise PaddleServingError(f"HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise PaddleServingError(f"cannot reach Paddle serving endpoint: {exc}") from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PaddleServingError("serving response is not valid JSON") from exc
        if not isinstance(value, dict):
            raise PaddleServingError("serving response must be a JSON object")
        return value

    def _save_binary(self, value: Any, path: Path) -> None:
        binary = self._decode_binary(value)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(binary)

    def _decode_binary(self, value: Any) -> bytes:
        if not isinstance(value, str):
            raise PaddleServingError("binary result is neither Base64 nor URL text")
        data_uri_match = DATA_URI_PATTERN.match(value)
        if data_uri_match:
            value = data_uri_match.group(1)
        if value.startswith(("http://", "https://")):
            headers = {"User-Agent": self.user_agent}
            if self.token:
                headers["Authorization"] = f"token {self.token}"
            request = Request(value, headers=headers)
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return self._read_response(response, "binary asset")
        try:
            return base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise PaddleServingError("binary result is not valid Base64") from exc

    @staticmethod
    def _read_response(response: Any, operation: str) -> bytes:
        try:
            return response.read()
        except IncompleteRead as exc:
            raise PaddleServingError(f"{operation} response was incomplete: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    configured = load_config().local_paddle
    parser = argparse.ArgumentParser(description="Send PDFs to remote PP-StructureV3 serving")
    parser.add_argument("--api-url", default=configured.api_url, required=False)
    parser.add_argument("--token", default=configured.token)
    parser.add_argument("--input", required=True, type=Path, help="one PDF or a directory of PDFs")
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=configured.timeout_seconds)
    parser.add_argument(
        "--page-batch-size",
        type=int,
        default=configured.page_batch_size,
        help="maximum PDF pages per Serving request; does not limit document size",
    )
    add_logging_arguments(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        configure_logging_from_args(args)
    except (OSError, ValueError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    log_event(
        LOGGER,
        "INFO",
        "command_started",
        command="remote.paddle_client",
        input_path=str(args.input),
        output_root=str(args.output_root),
    )
    if not args.api_url:
        log_event(
            LOGGER,
            "ERROR",
            "command_failed",
            command="remote.paddle_client",
            error_msg="PaddleOCR API URL is required",
        )
        print("error: --api-url or PADDLEOCR_API_URL is required", file=sys.stderr)
        return 2
    input_path = args.input
    pdfs = [input_path] if input_path.is_file() else sorted(input_path.rglob("*.pdf"))
    if not pdfs:
        log_event(
            LOGGER,
            "ERROR",
            "command_failed",
            command="remote.paddle_client",
            error_msg="no PDF files found",
            input_path=str(input_path),
        )
        print(f"error: no PDF files found under {input_path}", file=sys.stderr)
        return 2

    client = PaddleOCRClient(
        args.api_url,
        token=args.token,
        timeout_seconds=args.timeout,
        page_batch_size=args.page_batch_size,
    )
    failed = 0
    for pdf_path in pdfs:
        try:
            result = client.process_pdf(pdf_path, args.output_root / pdf_path.stem)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            log_event(
                LOGGER,
                "INFO",
                "ocr_document_finished",
                source_path=str(pdf_path),
                output_dir=result.get("output_dir"),
                page_result_count=result.get("page_result_count"),
            )
        except (OSError, ValueError, PaddleServingError) as exc:
            failed += 1
            log_event(
                LOGGER,
                "ERROR",
                "ocr_document_failed",
                source_path=str(pdf_path),
                error_msg=f"{type(exc).__name__}: {exc}",
            )
            print(f"failed: {pdf_path}: {type(exc).__name__}: {exc}", file=sys.stderr)
    log_event(
        LOGGER,
        "INFO" if not failed else "ERROR",
        "command_finished",
        command="remote.paddle_client",
        document_count=len(pdfs),
        failed=failed,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
