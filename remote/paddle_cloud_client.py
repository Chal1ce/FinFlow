"""Client for the official asynchronous PaddleOCR cloud API.

The cloud API accepts either a local PDF upload or a publicly reachable file
URL.  A submission returns a job ID; the result is materialized only after the
job reaches ``done`` and its JSONL result URL has been downloaded.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlparse

import requests

from config import load_config
from core.logging import add_logging_arguments, configure_logging_from_args, get_logger, log_event
from .paddle_client import PaddleOCRClient, _sha256_file, safe_relative_path


LOGGER = get_logger(__name__)


class PaddleOCRCloudError(RuntimeError):
    """Raised when the cloud submission, polling or result parsing fails."""


class PaddleOCRCloudTimeoutError(PaddleOCRCloudError):
    """Raised when a cloud job does not finish before the configured deadline."""


class PaddleOCRQueueBusyError(PaddleOCRCloudError):
    """Raised when the cloud job queue is full; submission can be retried."""


ProgressCallback = Callable[[str, Mapping[str, Any]], None]


class PaddleOCRCloudClient:
    """Submit PDFs to and download results from PaddleOCR's cloud API."""

    def __init__(
        self,
        job_url: str | None = None,
        *,
        token: str | None = None,
        model: str | None = None,
        optional_payload: Mapping[str, Any] | None = None,
        poll_interval_seconds: float | None = None,
        timeout_seconds: float | None = None,
        submit_retry_attempts: int | None = None,
        submit_retry_backoff_seconds: float | None = None,
        user_agent: str | None = None,
        session: requests.Session | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        configured = load_config().cloud_paddle
        self.job_url = (job_url or configured.job_url).rstrip("/")
        self.token = configured.token if token is None else token
        self.model = model or configured.model
        self.optional_payload = dict(
            configured.optional_payload if optional_payload is None else optional_payload
        )
        self.poll_interval_seconds = (
            configured.poll_interval_seconds
            if poll_interval_seconds is None
            else poll_interval_seconds
        )
        self.timeout_seconds = (
            configured.timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        self.submit_retry_attempts = (
            configured.submit_retry_attempts
            if submit_retry_attempts is None
            else submit_retry_attempts
        )
        self.submit_retry_backoff_seconds = (
            configured.submit_retry_backoff_seconds
            if submit_retry_backoff_seconds is None
            else submit_retry_backoff_seconds
        )
        self.user_agent = user_agent or configured.user_agent
        self.session = session or requests.Session()
        self.progress_callback = progress_callback

        if not self.job_url:
            raise ValueError("PaddleOCR cloud job URL is required")
        if not self.token:
            raise ValueError(
                "PaddleOCR cloud token is required; set PADDLEOCR_CLOUD_TOKEN"
            )
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than zero")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if self.submit_retry_attempts < 1:
            raise ValueError("submit_retry_attempts must be at least one")
        if self.submit_retry_backoff_seconds < 0:
            raise ValueError("submit_retry_backoff_seconds must not be negative")

    def submit_file(self, file_path: Path | str) -> str:
        """Upload one local file and return its asynchronous job ID."""

        source_path = Path(file_path)
        if not source_path.exists():
            raise FileNotFoundError(source_path)
        if not source_path.is_file():
            raise ValueError(f"input is not a file: {source_path}")

        data = {
            "model": self.model,
            "optionalPayload": json.dumps(self.optional_payload, ensure_ascii=False),
        }

        def submit() -> str:
            with source_path.open("rb") as handle:
                response = self._post_request(
                    self.job_url,
                    "job submission",
                    headers=self._headers(),
                    data=data,
                    files={"file": (source_path.name, handle, "application/pdf")},
                    timeout=self.timeout_seconds,
                )
            return self._job_id_from_response(response)

        return self._submit_with_retry(submit)

    def submit_url(self, file_url: str) -> str:
        """Submit a publicly reachable file URL and return its job ID."""

        if not file_url.startswith(("http://", "https://")):
            raise ValueError("file_url must use http:// or https://")
        payload = {
            "fileUrl": file_url,
            "model": self.model,
            "optionalPayload": self.optional_payload,
        }
        def submit() -> str:
            response = self._post_request(
                self.job_url,
                "job submission",
                headers=self._headers(content_type="application/json"),
                json=payload,
                timeout=self.timeout_seconds,
            )
            return self._job_id_from_response(response)

        return self._submit_with_retry(submit)

    def _submit_with_retry(self, operation: Callable[[], str]) -> str:
        """Submit a job and retry with backoff when the cloud queue is full."""

        last_error: PaddleOCRQueueBusyError | None = None
        for attempt in range(self.submit_retry_attempts):
            try:
                return operation()
            except PaddleOCRQueueBusyError as exc:
                last_error = exc
                if attempt + 1 >= self.submit_retry_attempts:
                    raise
                delay = min(
                    self.submit_retry_backoff_seconds * (2**attempt), 120.0
                )
                if delay > 0:
                    time.sleep(delay)
        if last_error is not None:
            raise last_error
        raise PaddleOCRCloudError("cloud job submission did not complete")

    def wait_for_result(self, job_id: str) -> str:
        """Poll one job until completion and return its JSONL result URL."""

        if not job_id:
            raise ValueError("job_id is required")

        deadline = time.monotonic() + self.timeout_seconds
        while True:
            if time.monotonic() >= deadline:
                raise PaddleOCRCloudTimeoutError(
                    f"job {job_id} did not finish within {self.timeout_seconds:g} seconds"
                )

            response = self._get_request(
                f"{self.job_url}/{job_id}",
                "job status",
                headers=self._headers(),
                timeout=self.timeout_seconds,
            )
            payload = self._json_response(response, "job status")
            data = payload.get("data")
            if not isinstance(data, dict):
                raise PaddleOCRCloudError("job status response does not contain data")

            state = str(data.get("state", "")).lower()
            self._notify_progress(state, data)
            if state == "done":
                result_url = data.get("resultUrl")
                if not isinstance(result_url, dict) or not result_url.get("jsonUrl"):
                    raise PaddleOCRCloudError(
                        "completed job response does not contain data.resultUrl.jsonUrl"
                    )
                return str(result_url["jsonUrl"])
            if state == "failed":
                error_message = data.get("errorMsg") or "unknown cloud OCR error"
                raise PaddleOCRCloudError(f"job {job_id} failed: {error_message}")
            if state not in {"pending", "running"}:
                raise PaddleOCRCloudError(f"unknown cloud OCR job state: {state!r}")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PaddleOCRCloudTimeoutError(
                    f"job {job_id} did not finish within {self.timeout_seconds:g} seconds"
                )
            time.sleep(min(self.poll_interval_seconds, remaining))

    def download_jsonl_result(
        self, jsonl_url: str, output_dir: Path | str
    ) -> dict[str, Any]:
        """Download JSONL and materialize Markdown pages and image assets."""

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        response = self._get_request(
            jsonl_url,
            "JSONL result",
            headers=self._result_headers(),
            timeout=self.timeout_seconds,
        )
        result_text = self._text_response(response, "JSONL result")
        (output_path / "result.jsonl").write_text(result_text, encoding="utf-8")

        markdown_parts: list[str] = []
        page_count = 0
        saved_image_count = 0
        compact_pages = []
        for line_number, line in enumerate(result_text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                line_payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PaddleOCRCloudError(
                    f"JSONL result has invalid JSON on line {line_number}"
                ) from exc
            result = line_payload.get("result") if isinstance(line_payload, dict) else None
            if not isinstance(result, dict):
                raise PaddleOCRCloudError(
                    f"JSONL result line {line_number} does not contain a result object"
                )

            pages = result.get("layoutParsingResults") or []
            if not isinstance(pages, list):
                raise PaddleOCRCloudError(
                    f"JSONL result line {line_number} has invalid layoutParsingResults"
                )
            for page in pages:
                if not isinstance(page, dict):
                    continue
                markdown = page.get("markdown") or {}
                image_assets = []
                if page.get("inputImage"):
                    input_path = output_path / "images" / "input" / f"page_{page_count+1:04d}.jpg"
                    self._save_asset(page["inputImage"], input_path)
                    image_assets.append({"source_key": "inputImage", "path": input_path.relative_to(output_path).as_posix(),
                                         "page": page_count+1, "purpose": "original_page", "sha256": _sha256_file(input_path)})
                    saved_image_count += 1
                page_text = ""
                if isinstance(markdown, dict):
                    page_text = str(markdown.get("text") or "")
                    images = markdown.get("images") or {}
                    if isinstance(images, dict):
                        for image_name, image_value in images.items():
                            image_path = (
                                output_path
                                / "images"
                                / "markdown"
                                / safe_relative_path(
                                    str(image_name), f"page_{page_count}_image.bin"
                                )
                            )
                            self._save_asset(image_value, image_path)
                            # Keep the legacy alias, and preserve a page-qualified canonical copy.
                            canonical = (output_path / "images" / "markdown" / f"page_{page_count+1:04d}"
                                         / safe_relative_path(str(image_name)))
                            canonical.parent.mkdir(parents=True, exist_ok=True)
                            canonical.write_bytes(image_path.read_bytes())
                            image_assets.append({"source_key": str(image_name), "path": canonical.relative_to(output_path).as_posix(),
                                                 "page": page_count+1, "purpose": "region", "sha256": _sha256_file(canonical)})
                            saved_image_count += 1

                output_images = page.get("outputImages") or {}
                if isinstance(output_images, dict):
                    for image_name, image_value in output_images.items():
                        safe_name = safe_relative_path(
                            str(image_name), f"page_{page_count}.jpg"
                        ).name
                        image_path = (
                            output_path / "images" / "output" / f"{safe_name}_{page_count}"
                        )
                        self._save_asset(image_value, image_path)
                        image_assets.append({"source_key": str(image_name), "path": image_path.relative_to(output_path).as_posix(),
                                             "page": page_count+1, "purpose": "page_or_debug", "sha256": _sha256_file(image_path)})
                        saved_image_count += 1

                page_count += 1
                compact = PaddleOCRClient._compact_page(page)
                compact["image_assets"] = image_assets
                compact_pages.append(compact)
                page_path = output_path / "pages" / f"page_{page_count:04d}.md"
                page_path.parent.mkdir(parents=True, exist_ok=True)
                page_path.write_text(page_text, encoding="utf-8")
                if page_text:
                    markdown_parts.append(page_text)

        markdown_path = output_path / "output.md"
        (output_path / "result.json").write_text(json.dumps({"layoutParsingResults": compact_pages}, ensure_ascii=False), encoding="utf-8")
        markdown_path.write_text("\n\n".join(markdown_parts), encoding="utf-8")
        return {
            "jsonl_path": str(output_path / "result.jsonl"),
            "markdown_path": str(markdown_path),
            "page_result_count": page_count,
            "saved_image_count": saved_image_count,
        }

    def process(
        self, input_path_or_url: Path | str, output_dir: Path | str
    ) -> dict[str, Any]:
        """Submit an input, wait for completion and materialize its result."""

        source = str(input_path_or_url)
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        if source.startswith(("http://", "https://")):
            job_id = self.submit_url(source)
        else:
            job_id = self.submit_file(Path(input_path_or_url))

        job_record = {"job_id": job_id, "source": source, "state": "submitted"}
        (output_path / "job.json").write_text(
            json.dumps(job_record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            jsonl_url = self.wait_for_result(job_id)
        except PaddleOCRCloudError as exc:
            job_record.update({"state": "failed", "error": str(exc)})
            (output_path / "job.json").write_text(
                json.dumps(job_record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            raise
        job_record.update({"state": "done", "jsonl_url": jsonl_url})
        (output_path / "job.json").write_text(
            json.dumps(job_record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        result = self.download_jsonl_result(jsonl_url, output_path)
        return {
            "job_id": job_id,
            "source": source,
            "output_dir": str(output_path),
            **result,
        }

    def _headers(self, *, content_type: str | None = None) -> dict[str, str]:
        """Build authenticated headers for the PaddleOCR job API."""

        headers = {
            "Accept": "application/json",
            "Authorization": f"bearer {self.token}",
            "User-Agent": self.user_agent,
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _result_headers(self) -> dict[str, str]:
        """Build unauthenticated headers for signed BOS result resources.

        The cloud job API returns short-lived BOS URLs for JSONL and image
        assets.  These URLs carry their own authorization in the query string;
        forwarding the cloud Bearer token makes BOS treat the request as a BCE
        authenticated request and reject it without a BCE date/signature.
        """

        return {"Accept": "*/*", "User-Agent": self.user_agent}

    def _post_request(self, url: str, operation: str, **kwargs: Any) -> requests.Response:
        try:
            return self.session.post(url, **kwargs)
        except requests.RequestException as exc:
            raise PaddleOCRCloudError(f"{operation} network request failed") from exc

    def _get_request(self, url: str, operation: str, **kwargs: Any) -> requests.Response:
        try:
            return self.session.get(url, **kwargs)
        except requests.RequestException as exc:
            raise PaddleOCRCloudError(f"{operation} network request failed") from exc

    def _job_id_from_response(self, response: requests.Response) -> str:
        payload = self._json_response(response, "job submission")
        code = payload.get("code")
        message = str(payload.get("msg") or "")
        if code == 10010 or "队列" in message or "queue" in message.lower():
            raise PaddleOCRQueueBusyError(message or "cloud job queue is full")
        data = payload.get("data")
        if not isinstance(data, dict) or not data.get("jobId"):
            raise PaddleOCRCloudError("job submission response does not contain data.jobId")
        return str(data["jobId"])

    def _json_response(self, response: requests.Response, operation: str) -> dict[str, Any]:
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            detail = response.text[:1000] if response.text else ""
            suffix = f": {detail}" if detail else ""
            raise PaddleOCRCloudError(f"{operation} HTTP request failed{suffix}") from exc
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise PaddleOCRCloudError(f"{operation} response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise PaddleOCRCloudError(f"{operation} response must be a JSON object")
        return payload

    def _text_response(self, response: requests.Response, operation: str) -> str:
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            detail = response.text[:1000] if response.text else ""
            suffix = f": {detail}" if detail else ""
            raise PaddleOCRCloudError(f"{operation} HTTP request failed{suffix}") from exc
        return response.text

    def _save_asset(self, value: Any, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self._decode_asset(value))

    def _decode_asset(self, value: Any) -> bytes:
        if isinstance(value, bytes):
            return value
        if not isinstance(value, str):
            raise PaddleOCRCloudError("cloud image result is neither Base64 nor URL text")
        if value.startswith(("http://", "https://")):
            response = self._get_request(
                value,
                "image download",
                headers=self._result_headers(),
                timeout=self.timeout_seconds,
            )
            return self._binary_response(response, "image download")

        encoded = value
        if value.startswith("data:") and ";base64," in value:
            encoded = value.split(";base64,", 1)[1]
        try:
            return base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise PaddleOCRCloudError("cloud image result is not valid Base64") from exc

    def _binary_response(self, response: requests.Response, operation: str) -> bytes:
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise PaddleOCRCloudError(f"{operation} HTTP request failed") from exc
        return response.content

    def _notify_progress(self, state: str, data: Mapping[str, Any]) -> None:
        if self.progress_callback:
            self.progress_callback(state, data)


def _output_name(source: str) -> str:
    if source.startswith(("http://", "https://")):
        name = Path(unquote(urlparse(source).path)).stem
        return name or "document"
    return Path(source).stem


def build_parser() -> argparse.ArgumentParser:
    configured = load_config()
    parser = argparse.ArgumentParser(
        description="Send a PDF to the official PaddleOCR cloud API"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="local PDF path")
    source.add_argument("--file-url", help="publicly reachable PDF URL")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=configured.paths.parsed_md / "cloud",
        help="root directory for derived OCR results",
    )
    parser.add_argument("--job-url", default=configured.cloud_paddle.job_url)
    parser.add_argument("--token", default=configured.cloud_paddle.token)
    parser.add_argument("--model", default=configured.cloud_paddle.model)
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=configured.cloud_paddle.poll_interval_seconds,
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=configured.cloud_paddle.timeout_seconds,
    )
    parser.add_argument(
        "--submit-retries",
        type=int,
        default=configured.cloud_paddle.submit_retry_attempts,
    )
    parser.add_argument(
        "--submit-backoff",
        type=float,
        default=configured.cloud_paddle.submit_retry_backoff_seconds,
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
    source = str(args.input) if args.input else args.file_url
    log_event(
        LOGGER,
        "INFO",
        "command_started",
        command="remote.paddle_cloud_client",
        source=source,
        output_root=str(args.output_root),
        model=args.model,
    )
    if not args.token:
        log_event(
            LOGGER,
            "ERROR",
            "command_failed",
            command="remote.paddle_cloud_client",
            error_msg="PaddleOCR cloud token is required",
        )
        print(
            "error: --token or PADDLEOCR_CLOUD_TOKEN is required; "
            "create a new token if the one previously pasted was real",
            file=sys.stderr,
        )
        return 2

    output_dir = args.output_root / _output_name(source)
    try:
        client = PaddleOCRCloudClient(
            args.job_url,
            token=args.token,
            model=args.model,
            poll_interval_seconds=args.poll_interval,
            timeout_seconds=args.timeout,
            submit_retry_attempts=args.submit_retries,
            submit_retry_backoff_seconds=args.submit_backoff,
        )
        result = client.process(source, output_dir)
    except (OSError, ValueError, PaddleOCRCloudError) as exc:
        log_event(
            LOGGER,
            "ERROR",
            "ocr_document_failed",
            source=source,
            error_msg=f"{type(exc).__name__}: {exc}",
        )
        print(f"failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    log_event(
        LOGGER,
        "INFO",
        "command_finished",
        command="remote.paddle_cloud_client",
        source=source,
        output_dir=result.get("output_dir"),
        page_result_count=result.get("page_result_count"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
