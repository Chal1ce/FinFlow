"""Preview or publish deterministic mixtures; no model calls in plan/build."""

import argparse
import json
from pathlib import Path

from training.mixture import build, plan, verify_mixture


def main(argv=None):
    parser = argparse.ArgumentParser(description="FinFlow training-data mixtures")
    parser.add_argument("--config", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="dry-run: report actual quotas and shortfalls without writing a dataset")
    publish = commands.add_parser("build")
    publish.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("verify")
    check.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            result = {"status": "success", "manifest": verify_mixture(args.path)}
        else:
            if args.config is None:
                raise ValueError("--config is required")
            config = json.loads(args.config.read_text(encoding="utf-8"))
            result = plan(config)[1] if args.command == "plan" else build(config, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("status") == "partial" else 0
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
