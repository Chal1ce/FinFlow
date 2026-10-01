"""Multi-source report collection orchestrator for cron jobs."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError

from core.context import PipelineContext
from core.logging import add_logging_arguments, configure_logging_from_args, get_logger, log_event
from qc.validators import QCCheck, qc_passed, validate_asset, validate_candidate
from storage.state_store import StateStore
from .downloader import ReportDownloader, utc_now
from .http_client import HttpClient, write_jsonl_atomic
from .source_adapters import (
    CollectionTarget,
    ReportCandidate,
    SourceAdapter,
    SourceDiscoveryError,
    build_source_adapters,
)


RETRYABLE_ERRORS = (HTTPError, URLError, TimeoutError, OSError, ValueError, SourceDiscoveryError)
LOGGER = get_logger(__name__)


class RunLock:
    """Portable O_EXCL lock preventing overlapping cron runs."""

    def __init__(self, path: Path, stale_after_seconds: float = 24 * 60 * 60) -> None:
        self.path = path
        self.stale_after_seconds = stale_after_seconds
        self._held = False

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError as exc:
            age = time.time() - self.path.stat().st_mtime
            if age > self.stale_after_seconds:
                self.path.unlink(missing_ok=True)
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            else:
                raise RuntimeError(f"another collection run is active: {self.path}") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()}\nstarted_at={utc_now()}\n")
        self._held = True
        return self

    def __exit__(self, *_args: object) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False


class CollectionRunner:
    """Discover candidates from all sources and download with fallback."""

    def __init__(
        self,
        data_root: Path | str,
        adapters: list[SourceAdapter],
        downloader: ReportDownloader,
        retry_attempts: int = 3,
        retry_backoff_seconds: float = 2.0,
        state_store: StateStore | None = None,
        min_pdf_size_bytes: int = 100,
    ) -> None:
        self.data_root = Path(data_root)
        self.adapters = adapters
        self.downloader = downloader
        self.retry_attempts = max(1, retry_attempts)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self.state_store = state_store
        self.min_pdf_size_bytes = min_pdf_size_bytes

    def run(
        self,
        targets: Iterable[CollectionTarget],
        dry_run: bool = False,
        *,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        context = PipelineContext.create(self.data_root, batch_id=batch_id)
        run_uid = context.run_id
        started_at = context.started_at
        target_list = list(targets)
        candidates: dict[str, ReportCandidate] = {}
        candidate_records_by_uid: dict[str, dict[str, Any]] = {}
        source_errors: list[dict[str, Any]] = []
        target_discovery: dict[str, list[str]] = {}

        lock_path = self.data_root / ".collector.lock"
        state_store = self.state_store or StateStore(self.data_root / "state" / "pipeline.db")
        owns_state_store = self.state_store is None
        with RunLock(lock_path):
            state_store.start_run(context, dry_run=dry_run)
            try:
                for target in target_list:
                    target_key = f"{target.stock_code}:{target.report_type}"
                    target_discovery[target_key] = []
                    for adapter in self.adapters:
                        step_id = state_store.start_step(
                            context,
                            f"discover:{adapter.name}",
                            target_key,
                            metadata={"stock_code": target.stock_code},
                        )
                        try:
                            discovered = self._with_retry(
                                lambda adapter=adapter, target=target: adapter.discover(target),
                                f"discover:{adapter.name}:{target.stock_code}",
                            )
                        except RETRYABLE_ERRORS as exc:
                            state_store.finish_step(step_id, "failed", f"{type(exc).__name__}: {exc}")
                            source_errors.append(
                                {
                                    "source_name": adapter.name,
                                    "stock_code": target.stock_code,
                                    "error_msg": f"{type(exc).__name__}: {exc}",
                                }
                            )
                            continue
                        state_store.finish_step(step_id, "success")
                        for candidate in discovered:
                            checks = validate_candidate(candidate)
                            state_store.record_qc(
                                context,
                                candidate.candidate_uid,
                                "candidate",
                                [check.as_mapping() for check in checks],
                            )
                            record = candidate.to_mapping()
                            record.update(
                                {
                                    "batch_id": context.batch_id,
                                    "run_id": context.run_id,
                                    "run_uid": context.run_id,
                                    "qc_status": "pass" if qc_passed(checks) else "fail",
                                }
                            )
                            candidate_records_by_uid[candidate.candidate_uid] = record
                            if qc_passed(checks):
                                candidates[candidate.candidate_uid] = candidate
                                target_discovery[target_key].append(candidate.candidate_uid)

                candidate_records = list(candidate_records_by_uid.values())
                write_jsonl_atomic(self.data_root / "discovery" / "candidates.jsonl", candidate_records)

                result_records: list[dict[str, Any]] = []
                for target in target_list:
                    target_candidates = [
                        candidates[candidate_uid]
                        for candidate_uid in target_discovery[f"{target.stock_code}:{target.report_type}"]
                        if candidate_uid in candidates
                    ]
                    result_records.extend(
                        self._collect_target(target, target_candidates, dry_run, context, state_store)
                    )

                summary = self._build_summary(
                    run_uid=run_uid,
                    batch_id=context.batch_id,
                    started_at=started_at,
                    target_list=target_list,
                    candidate_records=candidate_records,
                    result_records=result_records,
                    source_errors=source_errors,
                    dry_run=dry_run,
                )
                run_failed = bool(source_errors or summary["status_counts"].get("failed"))
                state_store.finish_run(run_uid, "failed" if run_failed else "success")
                self._write_summary(summary)
                for result in result_records:
                    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
                return summary
            except Exception as exc:
                state_store.finish_run(run_uid, "failed", f"{type(exc).__name__}: {exc}")
                raise
            finally:
                if owns_state_store:
                    state_store.close()

    def _collect_target(
        self,
        target: CollectionTarget,
        candidates: list[ReportCandidate],
        dry_run: bool,
        context: PipelineContext,
        state_store: StateStore,
    ) -> list[dict[str, Any]]:
        grouped: dict[str, list[ReportCandidate]] = {}
        for candidate in candidates:
            grouped.setdefault(candidate.canonical_uid, []).append(candidate)

        results: list[dict[str, Any]] = []
        for canonical_uid, group in sorted(grouped.items()):
            ordered = sorted(group, key=lambda item: (item.priority, item.source_name))
            attempts: list[dict[str, Any]] = []
            selected: ReportCandidate | None = None
            selected_result: dict[str, Any] | None = None

            if dry_run:
                selected = ordered[0] if ordered else None
                status = "discovered" if selected else "not_found"
            else:
                status = "failed"
                for candidate in ordered:
                    download_result = self._download_with_retry(candidate, context, state_store)
                    attempts.append(
                        {
                            "source_name": candidate.source_name,
                            "source_url": candidate.source_url,
                            "status": download_result.get("status"),
                            "error_msg": download_result.get("error_msg"),
                        }
                    )
                    if download_result.get("status") in {"success", "skipped", "duplicate"}:
                        selected = candidate
                        selected_result = download_result
                        status = str(download_result["status"])
                        break

            results.append(
                {
                    "run_uid": context.run_id,
                    "batch_id": context.batch_id,
                    "canonical_uid": canonical_uid,
                    "stock_code": target.stock_code,
                    "report_year": self._report_year_from_candidate(ordered),
                    "report_type": target.report_type,
                    "status": status,
                    "candidate_count": len(ordered),
                    "selected_source": selected.source_name if selected else None,
                    "selected_url": selected.source_url if selected else None,
                    "raw_path": selected_result.get("raw_path") if selected_result else None,
                    "attempts": attempts,
                }
            )
        if not grouped:
            results.append(
                {
                    "run_uid": context.run_id,
                    "batch_id": context.batch_id,
                    "canonical_uid": None,
                    "stock_code": target.stock_code,
                    "report_year": None,
                    "report_type": target.report_type,
                    "status": "not_found",
                    "candidate_count": 0,
                    "selected_source": None,
                    "selected_url": None,
                    "raw_path": None,
                    "attempts": [],
                }
            )
        return results

    def _download_with_retry(
        self, candidate: ReportCandidate, context: PipelineContext, state_store: StateStore
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for attempt in range(self.retry_attempts):
            step_id = state_store.start_step(
                context,
                "download",
                candidate.candidate_uid,
                metadata={"source_name": candidate.source_name, "source_url": candidate.source_url},
            )
            result = self.downloader.download(candidate.to_report_spec())
            if result.get("status") in {"success", "skipped", "duplicate"}:
                checks = validate_asset(
                    result,
                    self.data_root,
                    min_size_bytes=self.min_pdf_size_bytes,
                )
            else:
                checks = [
                    QCCheck(
                        "download_status",
                        "fail",
                        result.get("error_msg") or "download did not complete",
                        result.get("status"),
                    )
                ]
            entity_uid = str(result.get("asset_uid") or candidate.candidate_uid)
            state_store.record_qc(
                context,
                entity_uid,
                "raw_asset",
                [check.as_mapping() for check in checks],
            )
            if result.get("status") in {"success", "skipped", "duplicate"} and qc_passed(checks):
                associate_batch = getattr(self.downloader, "associate_batch", None)
                if callable(associate_batch):
                    associated = associate_batch(result, context.batch_id)
                    if associated is not None:
                        result = dict(associated)
                state_store.upsert_asset(context, result)
                state_store.finish_step(step_id, "success")
                return result
            state_store.upsert_asset(context, result)
            result = {
                **result,
                "status": "failed",
                "error_msg": "; ".join(
                    check.message or check.check_name for check in checks if check.status == "fail"
                )
                or result.get("error_msg")
                or "raw asset QC failed",
            }
            state_store.finish_step(step_id, "failed", result.get("error_msg"))
            if attempt + 1 < self.retry_attempts:
                self._backoff(attempt, f"download:{candidate.source_name}")
        return result

    def _with_retry(self, operation: Callable[[], list[ReportCandidate]], label: str) -> list[ReportCandidate]:
        for attempt in range(self.retry_attempts):
            try:
                return operation()
            except RETRYABLE_ERRORS:
                if attempt + 1 >= self.retry_attempts:
                    raise
                self._backoff(attempt, label)
        return []

    def _backoff(self, attempt: int, _label: str) -> None:
        delay = min(self.retry_backoff_seconds * (2**attempt), 60.0)
        if delay > 0:
            time.sleep(delay)

    @staticmethod
    def _report_year_from_candidate(candidates: list[ReportCandidate]) -> int | None:
        return candidates[0].report_year if candidates else None

    def _build_summary(
        self,
        *,
        run_uid: str,
        batch_id: str,
        started_at: str,
        target_list: list[CollectionTarget],
        candidate_records: list[dict[str, Any]],
        result_records: list[dict[str, Any]],
        source_errors: list[dict[str, Any]],
        dry_run: bool,
    ) -> dict[str, Any]:
        status_counts: dict[str, int] = {}
        for record in result_records:
            status = str(record["status"])
            status_counts[status] = status_counts.get(status, 0) + 1
        return {
            "run_uid": run_uid,
            "batch_id": batch_id,
            "started_at": started_at,
            "finished_at": utc_now(),
            "dry_run": dry_run,
            "target_count": len(target_list),
            "candidate_count": len(candidate_records),
            "result_count": len(result_records),
            "status_counts": status_counts,
            "source_errors": source_errors,
        }

    def _write_summary(self, summary: Mapping[str, Any]) -> None:
        path = self.data_root / "runs" / f"{summary['run_uid']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
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

    @staticmethod
    def _run_uid() -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def load_config(path: Path | str) -> dict[str, Any]:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError("collection config must be a JSON object")
    return config


def build_runner(config: Mapping[str, Any], data_root_override: Path | None = None) -> tuple[CollectionRunner, list[CollectionTarget]]:
    data_root = data_root_override or Path(str(config.get("data_root", "data")))
    http_config = config.get("http") or {}
    http_client = HttpClient(
        timeout_seconds=float(http_config.get("timeout_seconds", 30)),
        user_agent=str(http_config.get("user_agent", "fin-doc-governance/0.1")),
        min_interval_seconds=float(http_config.get("min_interval_seconds", 0.5)),
    )
    adapters = build_source_adapters(config.get("sources") or [], http_client)
    downloader = ReportDownloader(
        data_root=data_root,
        timeout_seconds=float(http_config.get("timeout_seconds", 30)),
        user_agent=str(http_config.get("user_agent", "fin-doc-governance/0.1")),
        min_size_bytes=int(config.get("min_pdf_size_bytes", 100)),
    )
    retry_config = config.get("retry") or {}
    runner = CollectionRunner(
        data_root=data_root,
        adapters=adapters,
        downloader=downloader,
        retry_attempts=int(retry_config.get("attempts", 3)),
        retry_backoff_seconds=float(retry_config.get("backoff_seconds", 2)),
        min_pdf_size_bytes=int(config.get("min_pdf_size_bytes", 100)),
    )
    targets = [CollectionTarget.from_mapping(item) for item in config.get("targets") or []]
    if not targets:
        raise ValueError("collection config must contain at least one target")
    return runner, targets


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover and download reports from multiple sources")
    parser.add_argument("--config", type=Path, default=Path("config/collection.json"))
    parser.add_argument("--data-root", type=Path, help="override data_root from config")
    parser.add_argument(
        "--batch-id", help="reuse a business batch ID when resuming a local run"
    )
    parser.add_argument("--dry-run", action="store_true", help="discover candidates without downloading")
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
        command="spiders.collector",
        config_path=str(args.config),
        batch_id=args.batch_id,
        dry_run=args.dry_run,
    )
    try:
        config = load_config(args.config)
        runner, targets = build_runner(config, args.data_root)
        summary = runner.run(targets, dry_run=args.dry_run, batch_id=args.batch_id)
    except (OSError, ValueError, RuntimeError) as exc:
        log_event(
            LOGGER,
            "ERROR",
            "command_failed",
            command="spiders.collector",
            error_msg=f"{type(exc).__name__}: {exc}",
        )
        print(f"error: {type(exc).__name__}: {exc}")
        return 2
    failed = summary["status_counts"].get("failed", 0)
    not_found = summary["status_counts"].get("not_found", 0)
    log_event(
        LOGGER,
        "INFO" if not failed and not not_found else "WARNING",
        "command_finished",
        command="spiders.collector",
        batch_id=summary.get("batch_id"),
        status=summary.get("status"),
        failed=failed,
        not_found=not_found,
    )
    return 1 if failed or not_found else 0


if __name__ == "__main__":
    raise SystemExit(main())
