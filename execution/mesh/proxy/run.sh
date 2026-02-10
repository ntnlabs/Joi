#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export MESH_BIND_HOST=${MESH_BIND_HOST:-0.0.0.0}
export MESH_BIND_PORT=${MESH_BIND_PORT:-8444}

exec python3 -m uvicorn --app-dir "$SCRIPT_DIR" server:app --host "$MESH_BIND_HOST" --port "$MESH_BIND_PORT"
