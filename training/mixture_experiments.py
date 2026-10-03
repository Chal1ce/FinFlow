"""Explicit, resumable local DoReMi/RegMix experiments; prepare needs no PyTorch."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path

from training.mixture_prepare import prepare
from workflow.flywheel import DailyLock


def main(argv=None):
    parser = argparse.ArgumentParser(description="Learn CPT data mixtures using real local proxy training")
    parser.add_argument("--config", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight", help="inspect local paths/dependency availability; no training or downloads")
    data = commands.add_parser("prepare")
    data.add_argument("--output", type=Path, required=True)
    for name in ("doremi", "regmix"):
        command = commands.add_parser(name)
        command.add_argument("--data", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
        if name == "regmix":
            command.add_argument("--max-trials", type=int, help="maximum newly completed design trials this invocation")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        if config.get("schema_version") != "finflow-mixture-experiment-v1":
            raise ValueError("expected finflow-mixture-experiment-v1")
        if args.command == "preflight":
            dependencies = {k: importlib.util.find_spec(k) is not None for k in ("torch", "numpy", "lightgbm", "tokenizers")}
            inputs = {str(p): Path(p).is_dir() for p in config.get("inputs", [])}
            tokenizer = Path(config.get("tokenizer", "")).is_file()
            result = {
                "status": "success" if inputs and all(inputs.values()) and tokenizer and all(dependencies.values()) else "failed",
                "dependencies": dependencies,
                "inputs": inputs,
                "tokenizer_exists": tokenizer,
                "scope": "paths and dependency presence only; prepare/train perform semantic validation",
                "network_requested": False,
                "training_started": False,
            }
        elif args.command == "prepare":
            result = prepare(config, args.output)
        else:
            os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
            with DailyLock(args.output):
                if args.command == "doremi":
                    from training.mixture_proxy import doremi

                    result = doremi(config, args.data, args.output)
                else:
                    from training.mixture_regmix import run

                    result = run(config, args.data, args.output, max_trials=args.max_trials)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result["status"] == "failed" else 1 if result["status"] == "partial" else 0
    except (Exception, KeyboardInterrupt) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc)
                    if isinstance(exc, (ValueError, ImportError, OSError))
                    else "inspect local experiment files; rerun the same configuration to resume",
                },
                ensure_ascii=False,
            )
        )
        return 130 if isinstance(exc, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
