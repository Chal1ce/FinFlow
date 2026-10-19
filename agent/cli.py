"""Financial agent assets and explicitly launched model training."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from agent.adapters.ocr_inventory import InventoryOptions, build_inventory, verify_inventory


def main(argv=None):
    parser = argparse.ArgumentParser(description="FinFlow agent data and training")
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("inventory", help="read-only OCR media audit and pending human review queue")
    inventory.add_argument("--input", type=Path, required=True, help="OCR output directory, result file, or parent of OCR outputs")
    inventory.add_argument("--output", type=Path, required=True, help="new immutable inventory directory, outside OCR outputs")
    inventory.add_argument("--max-documents", type=int)
    inventory.add_argument("--max-pages", type=int, help="maximum pages per selected document; limits are recorded")
    inventory.add_argument("--review-limit", type=int, default=20)
    inventory.add_argument("--max-result-bytes", type=int, default=268435456)
    inventory.add_argument("--max-image-bytes", type=int, default=10485760)
    inventory.add_argument("--max-image-pixels", type=int, default=40000000)
    inventory.add_argument(
        "--copy-media", action="store_true", help="copy decoded media bytes into the output; source files remain unchanged"
    )
    verify = commands.add_parser("verify-inventory", help="verify a produced audit's checksums, media bindings and counts")
    verify.add_argument("path", type=Path)
    assets = commands.add_parser("import-assets", help="reuse governed v7 evidence and existing SFT decisions for agents")
    assets.add_argument("--data-root", type=Path, required=True)
    assets.add_argument("--release", required=True, help="existing release directory name under published/flywheel")
    assets.add_argument("--output", type=Path, required=True, help="new agent asset directory")
    assets.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config" / "sft-vision.json")
    assets.add_argument("--flywheel-config", type=Path, default=Path(__file__).resolve().parents[1] / "config" / "flywheel.json")
    sft = commands.add_parser("sft", help="train LoRA with existing text/vision SFT exports on one GPU")
    sft.add_argument("--dataset", type=Path, required=True)
    sft.add_argument("--output", type=Path, required=True)
    sft.add_argument("--config", type=Path)
    sft.add_argument("--model", help="model ID or local model directory")
    sft.add_argument("--modality", choices=["all", "text", "vision"], default="all")
    sft.add_argument("--max-steps", type=int)
    sft.add_argument("--max-samples", type=int)
    sft.add_argument("--resume-from-checkpoint", type=Path)
    sft.add_argument("--prepare-only", action="store_true", help="serialize inputs and command without starting training")
    rl_build = commands.add_parser("rl-build", help="build executable synthetic research tasks for first GRPO training")
    rl_build.add_argument("--output", type=Path, required=True)
    rl_build.add_argument("--train-cases", type=int, default=128)
    rl_build.add_argument("--validation-cases", type=int, default=32)
    rl_build.add_argument("--seed", type=int, default=42)
    rl_sft = commands.add_parser("rl-sft", help="execute reference tools and export offline Agent SFT demonstrations")
    rl_sft.add_argument("--dataset", type=Path, required=True)
    rl_sft.add_argument("--output", type=Path, required=True)
    rl = commands.add_parser("rl", help="train single-GPU multi-turn financial research GRPO")
    rl.add_argument("--dataset", type=Path, required=True)
    rl.add_argument("--output", type=Path, required=True)
    rl.add_argument("--config", type=Path)
    rl.add_argument("--model", help="model ID or local model directory")
    rl.add_argument("--max-steps", type=int)
    rl.add_argument("--sft-adapter", type=Path, help="initialize a new RL run from an SFT LoRA adapter")
    rl.add_argument("--resume-from-checkpoint", type=Path)
    rl.add_argument("--prepare-only", action="store_true", help="serialize the command and frozen cases without starting training")
    rl_report = commands.add_parser("rl-report", help="summarize saved episode outcomes and failure reasons")
    rl_report.add_argument("--run", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "rl-sft":
            from agent.datasets.research_sft import export_research_sft

            result = export_research_sft(args.dataset, args.output)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "rl-report":
            from agent.trainers.rl.report import summarize

            print(json.dumps(summarize(args.run), ensure_ascii=False, indent=2))
            return 0
        if args.command == "rl-build":
            from agent.datasets.research import build_research_dataset

            result = build_research_dataset(
                args.output, train_cases=args.train_cases, validation_cases=args.validation_cases, seed=args.seed
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "rl":
            from agent.trainers.rl.runner import DEFAULT_CONFIG as RL_CONFIG, run_rl

            result = run_rl(
                args.dataset,
                args.output,
                config=args.config or RL_CONFIG,
                model=args.model,
                max_steps=args.max_steps,
                sft_adapter=args.sft_adapter,
                resume_from_checkpoint=args.resume_from_checkpoint,
                prepare_only=args.prepare_only,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] != "failed" else 1
        if args.command == "sft":
            from agent.trainers.sft.runner import DEFAULT_CONFIG, run_sft

            result = run_sft(
                args.dataset,
                args.output,
                config=args.config or DEFAULT_CONFIG,
                model=args.model,
                modality=args.modality,
                max_steps=args.max_steps,
                max_samples=args.max_samples,
                resume_from_checkpoint=args.resume_from_checkpoint,
                prepare_only=args.prepare_only,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] != "failed" else 1
        if args.command == "import-assets":
            from agent.adapters.visual_assets import import_assets

            policy = json.loads(args.config.read_text(encoding="utf-8"))
            source_policy = json.loads(args.flywheel_config.read_text(encoding="utf-8")).get("source_policy", {})
            manifest = import_assets(args.data_root, args.release, args.output, source_policy=source_policy, policy=policy)
            print(json.dumps({**manifest, "output": str(args.output.resolve())}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "verify-inventory":
            manifest = verify_inventory(args.path)
            print(
                json.dumps(
                    {
                        "status": "verified",
                        "inventory_id": manifest["inventory_id"],
                        "files_checked": len(manifest["files"]),
                        "summary": manifest["summary"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        options = InventoryOptions(**{key: getattr(args, key) for key in InventoryOptions.__dataclass_fields__})
        manifest = build_inventory(args.input, args.output, options)
        print(
            json.dumps(
                {
                    "status": manifest["status"],
                    "inventory_id": manifest["inventory_id"],
                    "output": str(args.output.resolve()),
                    "summary": manifest["summary"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1 if manifest["status"] == "partial" else 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "local agent data file operation failed"
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "message": message}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
