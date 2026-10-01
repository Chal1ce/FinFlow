#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
cd "$PROJECT_ROOT"
mkdir -p logs
if [ -z "${PYTHON_BIN:-}" ]; then
    if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
        PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
    elif [ -x "$PROJECT_ROOT/.venv-flywheel/bin/python" ]; then
        PYTHON_BIN="$PROJECT_ROOT/.venv-flywheel/bin/python"
    else
        PYTHON_BIN=python3
    fi
fi
# Python loads .env as data; model URLs and JSON values are never executed by a shell.
exec "$PYTHON_BIN" -m workflow.flywheel_cli run "$@" >> logs/flywheel.log 2>&1
