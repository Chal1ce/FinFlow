"""Index versioned training packages, then freeze near-duplicate decisions."""

import argparse
import json
import sqlite3
from pathlib import Path

from training.near_dedup import index_packages, scan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("index")
    add.add_argument("--inputs", nargs="+", required=True)
    check = commands.add_parser("scan")
    check.add_argument("--inputs", nargs="+", required=True)
    check.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        options = json.loads(args.config.read_text(encoding="utf-8")) if args.config else None
        result = (
            index_packages(args.inputs, args.index, options)
            if args.command == "index"
            else scan(args.inputs, args.index, args.output, options)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
