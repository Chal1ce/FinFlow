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
    rl_build.add_argument("--test-cases", type=int, default=0, help="frozen test cases; not used by training")
    rl_build.add_argument("--seed", type=int, default=42)
    rl_import = commands.add_parser("rl-import", help="build research tasks from already accepted SFT table transcriptions")
    rl_import.add_argument("--dataset", type=Path, required=True, help="existing governed financial-sft-dataset-v2 directory")
    rl_import.add_argument("--output", type=Path, required=True)
    rl_start = commands.add_parser("rl-start", help="prepare a zero-checkpoint experiment; optionally run Agent SFT -> GRPO")
    rl_start.add_argument("--output", type=Path, required=True)
    source = rl_start.add_mutually_exclusive_group()
    source.add_argument("--dataset", type=Path, help="existing executable research tasks; frozen into this experiment")
    source.add_argument("--sft-dataset", type=Path, help="existing governed SFT snapshot with accepted table_structure samples")
    rl_start.add_argument("--model", help="base model ID or local directory; defaults to Qwen/Qwen3.5-0.8B")
    rl_start.add_argument("--sft-steps", type=int)
    rl_start.add_argument("--rl-steps", type=int)
    rl_start.add_argument("--train-cases", type=int, help="synthetic fallback only; default 128")
    rl_start.add_argument("--validation-cases", type=int, help="synthetic fallback only; default 32")
    rl_start.add_argument("--seed", type=int, help="synthetic fallback only; default 42")
    rl_start.add_argument("--direct-rl", action="store_true", help="skip Agent SFT and initialize RL from the base model")
    rl_start.add_argument("--train", action="store_true", help="start GPU training; without this flag only prepare local data")
    rl_sft = commands.add_parser("rl-sft", help="export executed reference demonstrations or successful on-policy episodes")
    rl_sft.add_argument("--dataset", type=Path, required=True)
    rl_sft.add_argument("--output", type=Path, required=True)
    rl_sft.add_argument("--variants", type=int, default=2, help="reference variants per case (1-4)")
    rl_sft.add_argument("--from-episodes", type=Path, help="export successful train episodes from this run/episodes directory")
    rl_sft.add_argument("--per-case", type=int, default=1, help="maximum successful episodes per case")
    rl = commands.add_parser("rl", help="train single-GPU multi-turn financial research GRPO")
    rl.add_argument("--dataset", type=Path, required=True)
    rl.add_argument("--output", type=Path, required=True)
    rl.add_argument("--config", type=Path)
    rl.add_argument("--model", help="model ID or local model directory")
    rl.add_argument("--max-steps", type=int)
    rl.add_argument("--sft-adapter", type=Path, help="initialize a new RL run from an SFT LoRA adapter")
    rl.add_argument("--resume-from-checkpoint", type=Path)
    rl.add_argument("--prepare-only", action="store_true", help="serialize the command and frozen cases without starting training")
    rl_eval = commands.add_parser("rl-eval", help="run a frozen split with reference, replay or local-model policy")
    rl_eval.add_argument("--dataset", type=Path, required=True)
    rl_eval.add_argument("--output", type=Path, required=True, help="new evaluation directory")
    rl_eval.add_argument("--split", default="test", help="test (default), validation or train")
    rl_eval.add_argument("--policy", choices=["reference", "replay", "model"], default="reference")
    rl_eval.add_argument("--responses", type=Path, help="JSONL rows with case_id and actions for --policy replay")
    rl_eval.add_argument("--model", help="base model for --policy model")
    rl_eval.add_argument("--adapter", type=Path, help="optional SFT/RL LoRA adapter for --policy model")
    rl_eval.add_argument("--samples", type=int, default=1, help="runs per case; use 4 for pass@4")
    rl_eval.add_argument("--limit", type=int)
    rl_eval.add_argument("--max-new-tokens", type=int, default=512)
    rl_eval.add_argument("--temperature", type=float, default=0.9)
    rl_eval.add_argument("--top-p", type=float, default=0.95)
    rl_eval.add_argument("--allow-extra-evidence", action="store_true", help="accept any supporting evidence set instead of exact evidence")
    rl_report = commands.add_parser("rl-report", help="summarize saved episode outcomes and failure reasons")
    rl_report.add_argument("--run", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "rl-start":
            from agent.pipeline import start_research

            result = start_research(**{key: value for key, value in vars(args).items() if key != "command"})
            summary = {key: value for key, value in result.items() if key != "inputs"}
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0 if result["status"] != "failed" else 1
        if args.command == "rl-import":
            from agent.datasets.governed import build_governed_dataset

            print(json.dumps(build_governed_dataset(args.dataset, args.output), ensure_ascii=False, indent=2))
            return 0
        if args.command == "rl-sft":
            from agent.datasets.research_sft import export_episode_sft, export_research_sft

            if args.from_episodes:
                result = export_episode_sft(args.dataset, args.from_episodes, args.output, per_case=args.per_case)
            else:
                result = export_research_sft(args.dataset, args.output, variants=args.variants)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "rl-report":
            from agent.trainers.rl.report import summarize

            print(json.dumps(summarize(args.run), ensure_ascii=False, indent=2))
            return 0
        if args.command == "rl-eval":
            from agent.datasets.files import load_cases
            from agent.evaluation.policies import ReferencePolicy, ReplayPolicy
            from agent.evaluation.runner import evaluate, load_replay

            metadata = {}
            if args.policy == "reference":
                if args.adapter:
                    raise ValueError("--adapter is only valid with --policy model")
                factory = ReferencePolicy
            elif args.policy == "replay":
                if not args.responses or not args.responses.is_file():
                    raise ValueError("--policy replay requires an existing --responses JSONL file")
                actions = load_replay(args.responses)
                missing = [
                    case.manifest["case_id"]
                    for split, _, case in load_cases(args.dataset)
                    if split == args.split and case.manifest["case_id"] not in actions
                ]
                if missing:
                    raise ValueError("replay file is missing cases: " + ", ".join(missing[:3]))

                def factory(case):
                    return ReplayPolicy(case, actions[case.manifest["case_id"]])
            else:
                if not args.model:
                    raise ValueError("--policy model requires --model")
                from agent.evaluation.model_policy import ModelPolicy

                policy = ModelPolicy(
                    args.model,
                    adapter=str(args.adapter) if args.adapter else None,
                    max_new_tokens=args.max_new_tokens,
                    temperature=args.temperature,
                    top_p=args.top_p,
                )

                def factory(case):
                    return policy

                metadata = {
                    "model": args.model,
                    "adapter": str(args.adapter.resolve()) if args.adapter else None,
                    "max_new_tokens": args.max_new_tokens,
                    "temperature": args.temperature,
                    "top_p": args.top_p,
                }
            result = evaluate(
                args.dataset,
                args.output,
                policy_factory=factory,
                policy_name=args.policy,
                policy_metadata=metadata,
                split=args.split,
                samples=args.samples,
                limit=args.limit,
                strict_evidence=not args.allow_extra_evidence,
            )
            print(
                json.dumps(
                    {
                        "status": "completed",
                        "output": str(args.output.resolve()),
                        "split": args.split,
                        "policy": args.policy,
                        "samples": args.samples,
                        "summary": result["summary"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "rl-build":
            from agent.datasets.research import build_research_dataset

            result = build_research_dataset(
                args.output,
                train_cases=args.train_cases,
                validation_cases=args.validation_cases,
                test_cases=args.test_cases,
                seed=args.seed,
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
    except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "local agent data file operation failed"
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "message": message}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
