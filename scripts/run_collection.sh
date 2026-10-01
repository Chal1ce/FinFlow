#!/bin/sh
# Compatibility entrypoint: the approved daily flywheel now owns collection and downstream processing.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$SCRIPT_DIR/run_flywheel.sh" "$@"
