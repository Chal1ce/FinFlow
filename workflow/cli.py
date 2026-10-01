"""Command-line entrypoint for the local financial delivery workflow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.logging import add_logging_arguments, configure_logging_from_args, get_logger, log_event
from ingestion.local_documents import LocalDocumentOcrRunner, LocalPdfImporter
from processing.financial_metadata import (
    FinancialMetadataReviewService,
    FinancialMetadataRunner,
    FinancialReportVersionService,
    parse_metadata_override_assignments,
)
from qc.ocr_quality import OcrQualityReviewService, OcrQualityRunner
from .runner import LocalWorkflowRunner, WorkflowDefinitionError


LOGGER = get_logger(__name__)


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _add_document_metadata_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--document-id", help="stable logical document ID for local imports")
    parser.add_argument("--stock-code", help="six-digit stock code for a financial report")
    parser.add_argument("--report-year", type=int, help="reporting year for a financial report")
    parser.add_argument("--report-type", help="for example: annual, semiannual or research")
    parser.add_argument("--report-period", help="for example: 2024, 2024-H1 or 2024-Q3")
    parser.add_argument("--company-name", help="legal or commonly used company name")
    parser.add_argument("--announcement-date", help="announcement date in YYYY-MM-DD")
    parser.add_argument("--language", help="for example: zh-CN or en")
    parser.add_argument("--document-variant", help="for example: full, corrected or summary")
    parser.add_argument("--title", help="document title; defaults to the PDF filename")
    parser.add_argument("--source-name", help="source label; defaults to local-import")
    parser.add_argument("--source-id", help="source-local identifier")


def _document_metadata(args: argparse.Namespace) -> dict[str, object]:
    field_map = {
        "document_uid": "document_id",
        "stock_code": "stock_code",
        "report_year": "report_year",
        "report_type": "report_type",
        "report_period": "report_period",
        "company_name": "company_name",
        "announcement_date": "announcement_date",
        "language": "language",
        "document_variant": "document_variant",
        "title": "title",
        "source_name": "source_name",
        "source_id": "source_id",
    }
    return {
        target: getattr(args, source)
        for target, source in field_map.items()
        if getattr(args, source, None) is not None
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run, resume, validate and publish local financial document deliveries"
    )
    parser.add_argument("--data-root", type=Path, help="override the local data directory")
    parser.add_argument(
        "--collection-config", type=Path, help="collection config used by the governance stage"
    )
    parser.add_argument("--pipeline-config", type=Path, help="local workflow definitions")
    add_logging_arguments(parser)
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("list", help="list available local pipelines")

    run = subcommands.add_parser("run", help="run a local pipeline")
    run.add_argument("--pipeline", default="local_financial")
    run.add_argument("--batch-id")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--from-stage")
    run.add_argument("--until-stage")
    run.add_argument("--force", action="store_true", help="rebuild already governed documents")
    run.add_argument("--llm-backend", choices=("auto", "mock", "openai", "none"))
    run.add_argument("--input", type=Path, help="PDF or PDF directory required by ingest stages")
    run.add_argument("--ocr-backend", choices=("local", "cloud"))
    _add_document_metadata_arguments(run)

    resume = subcommands.add_parser("resume", help="resume a failed local workflow")
    resume.add_argument("workflow_run_id")
    resume.add_argument("--from-stage")
    resume.add_argument("--until-stage")
    resume.add_argument("--force", action="store_true")
    resume.add_argument("--llm-backend", choices=("auto", "mock", "openai", "none"))
    resume.add_argument("--ocr-backend", choices=("local", "cloud"))

    status = subcommands.add_parser("status", help="show workflow run status")
    status.add_argument("workflow_run_id")

    runs = subcommands.add_parser("runs", help="list recent local workflow runs")
    runs.add_argument("--limit", type=int, default=20)
    runs.add_argument("--batch-id")
    runs.add_argument("--status")

    overview = subcommands.add_parser("overview", help="show local operational overview")
    overview.add_argument("--limit", type=int, default=20)

    validate = subcommands.add_parser("validate", help="validate a batch before publishing")
    validate.add_argument("--batch-id", required=True)

    publish = subcommands.add_parser("publish", help="publish an immutable local release")
    publish.add_argument("--batch-id", required=True)
    publish.add_argument("--release-id", required=True)
    publish.add_argument("--dry-run", action="store_true")

    verify_release = subcommands.add_parser(
        "verify-release", help="verify an immutable release checksum manifest"
    )
    verify_release.add_argument("--batch-id", required=True)
    verify_release.add_argument("--release-id", required=True)

    backup_state = subcommands.add_parser(
        "backup-state", help="create a consistent SQLite state backup"
    )
    backup_state.add_argument("--output", type=Path, help="new SQLite backup path")

    ingest = subcommands.add_parser("ingest", help="import local PDFs into the data lake")
    ingest.add_argument("--input", required=True, type=Path)
    ingest.add_argument("--batch-id", required=True)
    ingest.add_argument("--dry-run", action="store_true")
    _add_document_metadata_arguments(ingest)

    ocr = subcommands.add_parser("ocr", help="run OCR for imported PDFs in one batch")
    ocr.add_argument("--batch-id", required=True)
    ocr.add_argument("--ocr-backend", required=True, choices=("local", "cloud"))
    ocr.add_argument("--dry-run", action="store_true")
    ocr.add_argument("--force", action="store_true")

    ocr_quality = subcommands.add_parser(
        "ocr-quality", help="assess OCR content quality without blocking publication"
    )
    ocr_quality.add_argument("--batch-id", required=True)
    ocr_quality.add_argument("--dry-run", action="store_true")
    ocr_quality.add_argument("--force", action="store_true")

    ocr_review = subcommands.add_parser(
        "ocr-review", help="list local OCR outputs that require inspection or retry"
    )
    ocr_review.add_argument("--batch-id", required=True)

    ocr_retry = subcommands.add_parser(
        "ocr-retry", help="archive and retry OCR for one local PDF asset"
    )
    ocr_retry.add_argument("--batch-id", required=True)
    ocr_retry.add_argument("--asset-id", required=True)
    ocr_retry.add_argument("--ocr-backend", required=True, choices=("local", "cloud"))
    ocr_retry.add_argument("--reason", required=True)

    metadata = subcommands.add_parser(
        "metadata", help="normalize financial metadata and emit non-blocking QC hints"
    )
    metadata.add_argument("--batch-id", required=True)
    metadata.add_argument("--dry-run", action="store_true")
    metadata.add_argument("--force", action="store_true")

    metadata_review = subcommands.add_parser(
        "metadata-review", help="list local financial metadata that needs human review"
    )
    metadata_review.add_argument("--batch-id", required=True)

    metadata_override = subcommands.add_parser(
        "metadata-override", help="apply an audited manual financial metadata correction"
    )
    metadata_override.add_argument("--batch-id", required=True)
    metadata_override.add_argument("--asset-id", required=True)
    metadata_override.add_argument(
        "--set",
        action="append",
        required=True,
        metavar="FIELD=VALUE",
        help="repeat for each metadata field to correct",
    )
    metadata_override.add_argument(
        "--reason", required=True, help="why the reviewed value is authoritative"
    )

    version_review = subcommands.add_parser(
        "version-review", help="list local report-version groups and pending choices"
    )
    version_review.add_argument("--batch-id", required=True)

    version_select = subcommands.add_parser(
        "version-select", help="select the effective PDF asset for one report-version group"
    )
    version_select.add_argument("--batch-id", required=True)
    version_select.add_argument("--report-group-id", required=True)
    version_select.add_argument("--asset-id", required=True)
    version_select.add_argument(
        "--reason", required=True, help="why this asset is the effective report version"
    )
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
        command="workflow.cli",
        operation=args.command,
        data_root=str(args.data_root or "data"),
    )
    log_event(
        LOGGER,
        "DEBUG",
        "command_arguments",
        command="workflow.cli",
        operation=args.command,
        arguments={key: str(value) for key, value in vars(args).items() if key != "log_file"},
    )
    try:
        runner = LocalWorkflowRunner(
            data_root=args.data_root,
            collection_config_path=args.collection_config,
            pipeline_config_path=args.pipeline_config,
        )
        if args.command == "list":
            _print(runner.list_pipelines())
            return 0
        if args.command == "run":
            result = runner.run(
                args.pipeline,
                batch_id=args.batch_id,
                dry_run=args.dry_run,
                from_stage=args.from_stage,
                until_stage=args.until_stage,
                force=args.force,
                llm_backend=args.llm_backend,
                input_path=args.input,
                ocr_backend=args.ocr_backend,
                document_metadata=_document_metadata(args),
            )
            _print(result)
            return 0 if result["status"] in {"success", "skipped"} else 1
        if args.command == "resume":
            previous = runner.status(args.workflow_run_id)
            result = runner.run(
                previous["pipeline_name"],
                batch_id=previous["batch_id"],
                dry_run=previous["dry_run"],
                from_stage=args.from_stage,
                until_stage=args.until_stage,
                force=args.force,
                llm_backend=args.llm_backend,
                ocr_backend=args.ocr_backend,
                resume_from_run_id=args.workflow_run_id,
            )
            _print(result)
            return 0 if result["status"] in {"success", "skipped"} else 1
        if args.command == "status":
            _print(runner.status(args.workflow_run_id))
            return 0
        if args.command == "runs":
            _print(runner.list_runs(limit=args.limit, batch_id=args.batch_id, status=args.status))
            return 0
        if args.command == "overview":
            _print(runner.overview(limit=args.limit))
            return 0
        if args.command == "validate":
            _print(runner.validate(args.batch_id))
            return 0
        if args.command == "publish":
            _print(runner.publish(args.batch_id, args.release_id, dry_run=args.dry_run))
            return 0
        if args.command == "verify-release":
            result = runner.verify_release(args.batch_id, args.release_id)
            _print(result)
            return 0 if result["status"] == "success" else 1
        if args.command == "backup-state":
            _print(runner.backup_state(args.output))
            return 0
        if args.command == "ingest":
            _print(
                LocalPdfImporter(runner.data_root).run(
                    args.input,
                    batch_id=args.batch_id,
                    metadata=_document_metadata(args),
                    dry_run=args.dry_run,
                )
            )
            return 0
        if args.command == "ocr":
            _print(
                LocalDocumentOcrRunner(runner.data_root).run(
                    batch_id=args.batch_id,
                    backend=args.ocr_backend,
                    dry_run=args.dry_run,
                    force=args.force,
                )
            )
            return 0
        if args.command == "ocr-quality":
            quality_result = OcrQualityRunner(runner.data_root).run(
                batch_id=args.batch_id, dry_run=args.dry_run, force=args.force
            )
            _print(quality_result)
            return 0 if quality_result["status"] == "success" else 1
        if args.command == "ocr-review":
            _print(OcrQualityReviewService(runner.data_root).review(batch_id=args.batch_id))
            return 0
        if args.command == "ocr-retry":
            ocr_result = LocalDocumentOcrRunner(runner.data_root).run(
                batch_id=args.batch_id,
                backend=args.ocr_backend,
                force=True,
                asset_uids=(args.asset_id,),
                retry_reason=args.reason,
            )
            quality_result = None
            if ocr_result["status"] == "success":
                quality_result = OcrQualityRunner(runner.data_root).run(
                    batch_id=args.batch_id, force=True
                )
            _print({"ocr": ocr_result, "quality": quality_result})
            return (
                0
                if ocr_result["status"] == "success"
                and quality_result is not None
                and quality_result["status"] == "success"
                else 1
            )
        if args.command == "metadata":
            _print(
                FinancialMetadataRunner(runner.data_root).run(
                    batch_id=args.batch_id, dry_run=args.dry_run, force=args.force
                )
            )
            return 0
        if args.command == "metadata-review":
            _print(FinancialMetadataReviewService(runner.data_root).review(batch_id=args.batch_id))
            return 0
        if args.command == "metadata-override":
            _print(
                FinancialMetadataReviewService(runner.data_root).apply_override(
                    batch_id=args.batch_id,
                    asset_uid=args.asset_id,
                    overrides=parse_metadata_override_assignments(args.set),
                    reason=args.reason,
                )
            )
            return 0
        if args.command == "version-review":
            _print(FinancialReportVersionService(runner.data_root).review(batch_id=args.batch_id))
            return 0
        if args.command == "version-select":
            _print(
                FinancialReportVersionService(runner.data_root).select(
                    batch_id=args.batch_id,
                    report_group_uid=args.report_group_id,
                    asset_uid=args.asset_id,
                    reason=args.reason,
                )
            )
            return 0
    except (OSError, RuntimeError, ValueError, WorkflowDefinitionError) as exc:
        log_event(
            LOGGER,
            "ERROR",
            "command_failed",
            command="workflow.cli",
            operation=args.command,
            error_msg=f"{type(exc).__name__}: {exc}",
        )
        print(f"error: {type(exc).__name__}: {exc}")
        return 2
    raise AssertionError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
