"""Cron-friendly incremental collector for Crossref and OpenAlex metadata."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError

from config import load_config as load_runtime_config
from core.context import PipelineContext
from core.logging import add_logging_arguments, configure_logging_from_args, get_logger, log_event
from qc.validators import qc_passed, validate_scholarly_candidate
from remote.paddle_client import PaddleOCRClient
from remote.paddle_cloud_client import (
    PaddleOCRCloudClient,
    PaddleOCRQueueBusyError,
)
from storage.state_store import StateStore
from .collector import RunLock
from .downloader import utc_now
from .http_client import HttpClient, write_jsonl_atomic
from .scholarly_downloader import ScholarlyPdfDownloader
from .scholarly_sources import (
    ScholarlyCandidate,
    ScholarlyDiscoveryError,
    ScholarlySourceAdapter,
    ScholarlyTarget,
    build_scholarly_adapters,
)


RETRYABLE_ERRORS = (HTTPError, URLError, TimeoutError, OSError, ValueError, ScholarlyDiscoveryError)
LOGGER = get_logger(__name__)


class ScholarlyCollector:
    """Discover and persist scholarly metadata without redownloading unchanged work."""

    def __init__(
        self,
        data_root: Path | str,
        adapters: list[ScholarlySourceAdapter],
        *,
        retry_attempts: int = 3,
        retry_backoff_seconds: float = 2.0,
        state_store: StateStore | None = None,
        pdf_downloader: ScholarlyPdfDownloader | None = None,
        ocr_processor: Callable[[Path, Path], Mapping[str, Any]] | None = None,
        ocr_root: Path | str | None = None,
        ocr_backend: str | None = None,
        ocr_max_per_run: int | None = None,
        ocr_delay_seconds: float = 0.0,
    ) -> None:
        if ocr_max_per_run is not None and ocr_max_per_run < 1:
            raise ValueError("ocr_max_per_run must be greater than zero")
        self.data_root = Path(data_root)
        self.adapters = adapters
        self.retry_attempts = max(1, retry_attempts)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self.state_store = state_store
        self.pdf_downloader = pdf_downloader
        self.ocr_processor = ocr_processor
        self.ocr_root = Path(ocr_root) if ocr_root else self.data_root / "parsed_md" / "scholarly"
        self.ocr_backend = ocr_backend
        self.ocr_max_per_run = ocr_max_per_run
        self.ocr_delay_seconds = max(0.0, ocr_delay_seconds)

    def run(
        self,
        targets: Iterable[ScholarlyTarget],
        *,
        dry_run: bool = False,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        context = PipelineContext.create(
            self.data_root, batch_id=batch_id, config_version="scholarly-v1"
        )
        target_list = list(targets)
        source_errors: list[dict[str, Any]] = []
        processing_errors: list[dict[str, Any]] = []
        records: list[dict[str, Any]] = []
        counts = {"new": 0, "changed": 0, "unchanged": 0, "preview": 0}
        pdf_counts: dict[str, int] = {}
        ocr_counts: dict[str, int] = {}
        ocr_attempted = 0
        ocr_deferred = False
        lock_path = self.data_root / ".scholarly.lock"
        state_store = self.state_store or StateStore(self.data_root / "state" / "pipeline.db")
        owns_state_store = self.state_store is None

        with RunLock(lock_path):
            state_store.start_run(context, dry_run=dry_run)
            try:
                for target in target_list:
                    for adapter in self.adapters:
                        sync_state = state_store.get_sync_state(adapter.name, target.target_uid)
                        updated_since = sync_state["last_success_at"] if sync_state else None
                        cursor = sync_state["cursor"] if sync_state else None
                        step_id = state_store.start_step(
                            context,
                            f"discover:{adapter.name}",
                            target.target_uid,
                            metadata={
                                "target_name": target.name,
                                "query": target.query,
                                "updated_since": updated_since,
                            },
                        )
                        try:
                            discovered = self._with_retry(
                                lambda adapter=adapter, target=target, updated_since=updated_since, cursor=cursor: adapter.discover(
                                    target, updated_since=updated_since, cursor=cursor
                                ),
                                f"discover:{adapter.name}:{target.name}",
                            )
                        except RETRYABLE_ERRORS as exc:
                            state_store.finish_step(step_id, "failed", f"{type(exc).__name__}: {exc}")
                            source_errors.append(
                                {
                                    "source_name": adapter.name,
                                    "target_name": target.name,
                                    "error_msg": f"{type(exc).__name__}: {exc}",
                                }
                            )
                            continue

                        state_store.finish_step(step_id, "success")
                        processing_failed = False
                        for candidate in discovered:
                            checks = validate_scholarly_candidate(candidate)
                            state_store.record_qc(
                                context,
                                candidate.candidate_uid,
                                "scholarly_candidate",
                                [check.as_mapping() for check in checks],
                            )
                            record = candidate.to_mapping()
                            record.update(
                                {
                                    "batch_id": context.batch_id,
                                    "run_id": context.run_id,
                                    "target_name": target.name,
                                    "target_uid": target.target_uid,
                                    "qc_status": "pass" if qc_passed(checks) else "fail",
                                }
                            )
                            if not qc_passed(checks):
                                record["change_status"] = "invalid"
                            elif dry_run:
                                record["change_status"] = "preview"
                            else:
                                record["change_status"] = state_store.upsert_scholarly_candidate(
                                    context, record
                                )
                            counts[record["change_status"]] = counts.get(
                                record["change_status"], 0
                            ) + 1

                            if self.pdf_downloader and not dry_run:
                                pdf_result = self.pdf_downloader.download(record)
                                record["pdf_status"] = pdf_result.get("status")
                                record["pdf_raw_path"] = pdf_result.get("raw_path")
                                record["pdf_named_path"] = pdf_result.get("named_path")
                                record["pdf_named_number"] = pdf_result.get("named_number")
                                record["asset_uid"] = pdf_result.get("asset_uid")
                                pdf_counts[str(pdf_result.get("status"))] = (
                                    pdf_counts.get(str(pdf_result.get("status")), 0) + 1
                                )
                                if pdf_result.get("asset_uid"):
                                    state_store.upsert_scholarly_asset(context, pdf_result)
                                if (
                                    pdf_result.get("status") in {"failed", "blocked"}
                                    and not pdf_result.get("skipped")
                                ):
                                    processing_failed = True
                                    processing_errors.append(
                                        {
                                            "stage": "download_pdf",
                                            "candidate_uid": candidate.candidate_uid,
                                            "error_msg": pdf_result.get("error_msg"),
                                            "failure_type": pdf_result.get("failure_type"),
                                            "retryable": pdf_result.get("failure_type")
                                            == "retryable",
                                        }
                                    )

                                if (
                                    self.ocr_processor
                                    and pdf_result.get("status") in {"success", "skipped", "duplicate"}
                                    and pdf_result.get("raw_path")
                                    and pdf_result.get("named_path")
                                ):
                                    named_relative = str(pdf_result["named_path"])
                                    record["ocr_input_path"] = named_relative
                                    asset_uid = str(pdf_result.get("asset_uid") or "asset")
                                    ocr_output = (
                                        self.ocr_root
                                        / candidate.work_uid
                                        / asset_uid
                                        / str(self.ocr_backend or "default")
                                    )
                                    ocr_output.mkdir(parents=True, exist_ok=True)
                                    (ocr_output / "input.json").write_text(
                                        json.dumps(
                                            {
                                                "run_id": context.run_id,
                                                "batch_id": context.batch_id,
                                                "candidate_uid": candidate.candidate_uid,
                                                "work_uid": candidate.work_uid,
                                                "source_name": candidate.source_name,
                                                "source_id": candidate.source_id,
                                                "pdf_url": pdf_result.get("pdf_url"),
                                                "asset_uid": asset_uid,
                                                "raw_path": pdf_result.get("raw_path"),
                                                "named_path": named_relative,
                                                "named_number": pdf_result.get("named_number"),
                                                "ocr_backend": self.ocr_backend,
                                                "ocr_output_dir": str(ocr_output),
                                                "written_at": utc_now(),
                                            },
                                            ensure_ascii=False,
                                            indent=2,
                                        )
                                        + "\n",
                                        encoding="utf-8",
                                    )
                                    if (
                                        pdf_result.get("status") in {"skipped", "duplicate"}
                                        and (ocr_output / "output.md").exists()
                                    ):
                                        record["ocr_status"] = "skipped"
                                    elif (
                                        self.ocr_max_per_run is not None
                                        and ocr_attempted >= self.ocr_max_per_run
                                    ):
                                        record["ocr_status"] = "deferred"
                                        ocr_deferred = True
                                    else:
                                        try:
                                            if self.ocr_delay_seconds > 0:
                                                time.sleep(self.ocr_delay_seconds)
                                            ocr_input = self.data_root / named_relative
                                            if not ocr_input.exists():
                                                raise ValueError(
                                                    f"named PDF copy is missing: {named_relative}"
                                                )
                                            ocr_result = self.ocr_processor(
                                                ocr_input,
                                                ocr_output,
                                            )
                                            ocr_attempted += 1
                                            record["ocr_status"] = "success"
                                            record["ocr_result"] = dict(ocr_result)
                                        except (OSError, RuntimeError, ValueError) as exc:
                                            retryable = isinstance(
                                                exc, PaddleOCRQueueBusyError
                                            )
                                            processing_failed = True
                                            record["ocr_status"] = (
                                                "pending_retry" if retryable else "failed"
                                            )
                                            record["ocr_error"] = f"{type(exc).__name__}: {exc}"
                                            processing_errors.append(
                                                {
                                                    "stage": "ocr",
                                                    "candidate_uid": candidate.candidate_uid,
                                                    "error_msg": record["ocr_error"],
                                                    "retryable": retryable,
                                                }
                                            )
                                elif self.ocr_processor:
                                    record["ocr_status"] = "not_available"
                                if record.get("ocr_status"):
                                    ocr_status = str(record["ocr_status"])
                                    ocr_counts[ocr_status] = ocr_counts.get(ocr_status, 0) + 1
                            else:
                                record["pdf_status"] = "preview" if dry_run else "not_requested"
                            records.append(record)

                        if not dry_run and not processing_failed and not ocr_deferred:
                            state_store.upsert_sync_state(
                                adapter.name,
                                target.target_uid,
                                last_success_at=utc_now(),
                                cursor=adapter.next_cursor,
                            )

                discovery_path = self.data_root / "discovery" / "scholarly_candidates.jsonl"
                write_jsonl_atomic(discovery_path, records)
                if source_errors:
                    run_status = "failed"
                elif processing_errors:
                    successful_work = any(
                        record.get("ocr_status") == "success"
                        or record.get("pdf_status")
                        in {"success", "skipped", "duplicate"}
                        for record in records
                    )
                    permanent_failures = [
                        item
                        for item in processing_errors
                        if not item.get("retryable")
                    ]
                    run_status = (
                        "failed"
                        if permanent_failures and not successful_work
                        else "partial"
                    )
                else:
                    run_status = "success"
                if run_status == "failed" and source_errors:
                    status_message = "one or more scholarly sources failed"
                elif run_status == "failed":
                    status_message = "one or more documents could not be processed"
                elif run_status == "partial":
                    status_message = "some work items need retry on the next run"
                else:
                    status_message = None
                summary = {
                    "run_uid": context.run_id,
                    "batch_id": context.batch_id,
                    "started_at": context.started_at,
                    "finished_at": utc_now(),
                    "dry_run": dry_run,
                    "target_count": len(target_list),
                    "source_count": len(self.adapters),
                    "candidate_count": len(records),
                    "work_count": len({record["work_uid"] for record in records}),
                    "change_counts": counts,
                    "pdf_status_counts": pdf_counts,
                    "ocr_status_counts": ocr_counts,
                    "ocr_deferred_count": ocr_counts.get("deferred", 0),
                    "source_errors": source_errors,
                    "processing_errors": processing_errors,
                    "discovery_path": str(discovery_path),
                    "status": run_status,
                }
                state_store.finish_run(
                    context.run_id,
                    summary["status"],
                    status_message,
                )
                self._write_summary(summary)
                print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
                return summary
            except Exception as exc:
                state_store.finish_run(context.run_id, "failed", f"{type(exc).__name__}: {exc}")
                raise
            finally:
                if owns_state_store:
                    state_store.close()

    def _with_retry(
        self, operation: Callable[[], list[ScholarlyCandidate]], label: str
    ) -> list[ScholarlyCandidate]:
        for attempt in range(self.retry_attempts):
            try:
                return operation()
            except RETRYABLE_ERRORS:
                if attempt + 1 >= self.retry_attempts:
                    raise
                delay = min(self.retry_backoff_seconds * (2**attempt), 60.0)
                if delay > 0:
                    time.sleep(delay)
        return []

    def _write_summary(self, summary: Mapping[str, Any]) -> None:
        path = self.data_root / "runs" / f"{summary['run_uid']}-scholarly.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(dict(summary), handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)


def load_config(path: Path | str) -> dict[str, Any]:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("collection config must be a JSON object")
    return value


def build_runner(
    config: Mapping[str, Any],
    data_root_override: Path | None = None,
    *,
    download_pdf_override: bool | None = None,
    ocr_backend: str | None = None,
    max_results_override: int | None = None,
    ocr_max_per_run_override: int | None = None,
) -> tuple[ScholarlyCollector, list[ScholarlyTarget]]:
    scholarly_config = config.get("scholarly") or {}
    if not scholarly_config.get("enabled", True):
        raise ValueError("scholarly collection is disabled")
    data_root = data_root_override or Path(str(config.get("data_root", "data")))
    http_config = config.get("http") or {}
    http_client = HttpClient(
        timeout_seconds=float(http_config.get("timeout_seconds", 30)),
        user_agent=str(http_config.get("user_agent", "fin-doc-governance/0.1")),
        min_interval_seconds=float(http_config.get("min_interval_seconds", 0.8)),
    )
    adapters = build_scholarly_adapters(scholarly_config.get("sources") or [], http_client)
    targets = [
        ScholarlyTarget.from_mapping(item)
        for item in scholarly_config.get("targets") or []
    ]
    if max_results_override is not None:
        if max_results_override < 1:
            raise ValueError("--max-results must be greater than zero")
        targets = [replace(target, max_results=max_results_override) for target in targets]
    if not adapters:
        raise ValueError("scholarly config must contain at least one enabled source")
    if not targets:
        raise ValueError("scholarly config must contain at least one target")
    retry_config = config.get("retry") or {}
    download_pdfs = bool(scholarly_config.get("download_open_access_pdfs", False))
    if download_pdf_override is not None:
        download_pdfs = download_pdf_override
    if ocr_backend and not download_pdfs:
        raise ValueError("--ocr-backend requires open-access PDF downloads")
    ocr_max_per_run = scholarly_config.get("ocr_max_per_run")
    if ocr_max_per_run is not None:
        ocr_max_per_run = int(ocr_max_per_run)
        if ocr_max_per_run < 1:
            raise ValueError("ocr_max_per_run must be greater than zero")
    if ocr_max_per_run_override is not None:
        if ocr_max_per_run_override < 1:
            raise ValueError("--ocr-max-per-run must be greater than zero")
        ocr_max_per_run = ocr_max_per_run_override
    ocr_delay_seconds = float(scholarly_config.get("ocr_delay_seconds", 0))
    pdf_downloader = (
        ScholarlyPdfDownloader(
            data_root,
            http_client=http_client,
            min_size_bytes=int(config.get("min_pdf_size_bytes", 100)),
            download_attempts=int(
                retry_config.get(
                    "pdf_attempts", retry_config.get("attempts", 3)
                )
            ),
            download_backoff_seconds=float(
                retry_config.get(
                    "pdf_backoff_seconds",
                    retry_config.get("backoff_seconds", 2),
                )
            ),
        )
        if download_pdfs
        else None
    )
    ocr_processor = None
    if ocr_backend:
        runtime = load_runtime_config()
        if ocr_backend == "local":
            if not runtime.local_paddle.api_url:
                raise ValueError(
                    "local OCR requires PADDLEOCR_LOCAL_API_URL or --api-url configuration"
                )
            local_client = PaddleOCRClient(
                runtime.local_paddle.api_url,
                token=runtime.local_paddle.token,
                timeout_seconds=runtime.local_paddle.timeout_seconds,
                user_agent=runtime.local_paddle.user_agent,
            )
            def process_local(pdf_path: Path, output_path: Path):
                return local_client.process_pdf(pdf_path, output_path)

            ocr_processor = process_local
        elif ocr_backend == "cloud":
            cloud_client = PaddleOCRCloudClient()

            def process_cloud(pdf_path: Path, output_path: Path):
                return cloud_client.process(pdf_path, output_path)

            ocr_processor = process_cloud
        else:
            raise ValueError("ocr_backend must be local or cloud")
    runner = ScholarlyCollector(
        data_root=data_root,
        adapters=adapters,
        retry_attempts=int(retry_config.get("attempts", 3)),
        retry_backoff_seconds=float(retry_config.get("backoff_seconds", 2)),
        pdf_downloader=pdf_downloader,
        ocr_processor=ocr_processor,
        ocr_root=scholarly_config.get("ocr_root") or data_root / "parsed_md" / "scholarly",
        ocr_backend=ocr_backend,
        ocr_max_per_run=ocr_max_per_run,
        ocr_delay_seconds=ocr_delay_seconds,
    )
    return runner, targets


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover scholarly metadata incrementally from Crossref and OpenAlex"
    )
    parser.add_argument("--config", type=Path, default=Path("config/collection.json"))
    parser.add_argument("--data-root", type=Path, help="override data_root from config")
    parser.add_argument(
        "--batch-id", help="reuse a business batch ID when resuming a local run"
    )
    parser.add_argument("--dry-run", action="store_true", help="discover without updating sync state")
    parser.add_argument(
        "--max-results",
        type=int,
        help="override max_results for all scholarly targets",
    )
    pdf_group = parser.add_mutually_exclusive_group()
    pdf_group.add_argument(
        "--download-pdf",
        dest="download_pdf",
        action="store_true",
        help="download explicitly open-access PDF URLs",
    )
    pdf_group.add_argument(
        "--no-download-pdf",
        dest="download_pdf",
        action="store_false",
        help="skip open-access PDF downloads",
    )
    parser.set_defaults(download_pdf=None)
    parser.add_argument(
        "--ocr-backend",
        choices=("local", "cloud"),
        help="send downloaded PDFs to the configured PaddleOCR backend",
    )
    parser.add_argument(
        "--ocr-max-per-run",
        type=int,
        help="limit OCR jobs submitted in one run",
    )
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
        command="spiders.scholarly_collector",
        config_path=str(args.config),
        batch_id=args.batch_id,
        dry_run=args.dry_run,
        ocr_backend=args.ocr_backend,
    )
    try:
        config = load_config(args.config)
        runner, targets = build_runner(
            config,
            args.data_root,
            download_pdf_override=args.download_pdf,
            ocr_backend=args.ocr_backend,
            max_results_override=args.max_results,
            ocr_max_per_run_override=args.ocr_max_per_run,
        )
        summary = runner.run(targets, dry_run=args.dry_run, batch_id=args.batch_id)
    except (OSError, ValueError, RuntimeError) as exc:
        log_event(
            LOGGER,
            "ERROR",
            "command_failed",
            command="spiders.scholarly_collector",
            error_msg=f"{type(exc).__name__}: {exc}",
        )
        print(f"error: {type(exc).__name__}: {exc}")
        return 2
    log_event(
        LOGGER,
        "INFO" if summary["status"] != "failed" else "ERROR",
        "command_finished",
        command="spiders.scholarly_collector",
        batch_id=summary.get("batch_id"),
        status=summary["status"],
    )
    return 1 if summary["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
