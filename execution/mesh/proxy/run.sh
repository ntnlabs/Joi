#!/usr/bin/env bash
set -euo pipefail

export MESH_BIND_HOST=${MESH_BIND_HOST:-0.0.0.0}
export MESH_BIND_PORT=${MESH_BIND_PORT:-8444}

python -m uvicorn server:app --host "$MESH_BIND_HOST" --port "$MESH_BIND_PORT"
