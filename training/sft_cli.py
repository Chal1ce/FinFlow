"""Generate SFT data without requiring OCR or a CPT tokenizer at this stage."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from config import load_config
from core.context import PipelineContext
from storage.flywheel_store import FlywheelStore
from training.flywheel_corpus import verify
from training.sft import SFTBuilder
from training.sft_config import SFTConfig
from workflow.flywheel import DailyLock


def main(argv=None):
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Evidence-grounded financial SFT datasets")
    parser.add_argument("--config", type=Path, default=root / "config" / "sft.json")
    parser.add_argument(
        "--flywheel-config",
        type=Path,
        default=root / "config" / "flywheel.json",
        help="read current source_policy; OCR/tokenizer configuration is not required",
    )
    parser.add_argument("--data-root", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight", help="local configuration checks only; no model calls")
    build = commands.add_parser("build", help="generate/reuse reviewed samples and publish an immutable snapshot")
    build.add_argument("--dataset-id", required=True)
    build.add_argument("--release", required=True, help="v7 release directory name under published/flywheel")
    check = commands.add_parser("verify")
    check.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            result = {"status": "success", "manifest": verify(args.path)}
        else:
            config = SFTConfig(args.config)
            source_policy = json.loads(args.flywheel_config.read_text(encoding="utf-8")).get("source_policy", {})
            if not isinstance(source_policy, dict) or any(not isinstance(v, dict) for v in source_policy.values()):
                raise ValueError("source_policy must map source names to policy objects")
            result = config.preflight()
            if args.command == "build" and not result["errors"]:
                data_root = (args.data_root or load_config().paths.data_root).resolve()
                context = PipelineContext.create(data_root, config_version=config.version, rule_version="sft-v1")
                with DailyLock(data_root), FlywheelStore(data_root / "state" / "pipeline.db") as store:
                    store.start_run(context, dry_run=False)
                    try:
                        result = SFTBuilder(data_root, store, context, config, source_policy).build(args.dataset_id, args.release)
                        store.finish_run(context.run_id, result["status"])
                    except BaseException as exc:
                        store.finish_run(context.run_id, "failed", type(exc).__name__)
                        raise
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result["status"] == "failed" else 1 if result["status"] == "partial" else 0
    except Exception as exc:
        # Provider exceptions may contain credentials or request contents.
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc) if isinstance(exc, (ValueError, FileNotFoundError)) else "check local SFT state",
                },
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
