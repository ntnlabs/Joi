#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXECUTION_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

export PYTHONPATH="$EXECUTION_DIR${PYTHONPATH:+:$PYTHONPATH}"

exec python3 "$SCRIPT_DIR/signal_worker.py"
