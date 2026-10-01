"""Command-line entrypoint for local continued-pretraining corpus generation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.logging import add_logging_arguments, configure_logging_from_args, get_logger, log_event
from .pretrain import PretrainDatasetBuilder, TrainingDataError


LOGGER = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a model-agnostic continued-pretraining corpus from verified releases"
    )
    parser.add_argument("--data-root", type=Path, default=Path("data"), help="local delivery data root")
    parser.add_argument("--output-root", type=Path, help="root directory for generated training datasets")
    add_logging_arguments(parser)
    subcommands = parser.add_subparsers(dest="command", required=True)

    build_pretrain = subcommands.add_parser(
        "build-pretrain", help="build an immutable continued-pretraining JSONL dataset"
    )
    build_pretrain.add_argument("--dataset-id", required=True, help="new dataset directory name")
    build_pretrain.add_argument(
        "--release",
        action="append",
        required=True,
        metavar="BATCH_ID/RELEASE_ID",
        help="repeat for every verified release to include",
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
        command="training.cli",
        operation=args.command,
        data_root=str(args.data_root),
    )
    try:
        if args.command == "build-pretrain":
            result = PretrainDatasetBuilder(args.data_root, output_root=args.output_root).build(
                dataset_id=args.dataset_id,
                releases=args.release,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
    except (OSError, RuntimeError, ValueError, TrainingDataError) as exc:
        log_event(
            LOGGER,
            "ERROR",
            "command_failed",
            command="training.cli",
            operation=args.command,
            error_msg=f"{type(exc).__name__}: {exc}",
        )
        print(f"error: {type(exc).__name__}: {exc}")
        return 2
    raise AssertionError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
