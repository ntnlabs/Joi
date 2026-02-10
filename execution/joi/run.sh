#!/bin/bash
set -e

cd "$(dirname "$0")"

# Default settings (override via environment)
export JOI_BIND_HOST="${JOI_BIND_HOST:-0.0.0.0}"
export JOI_BIND_PORT="${JOI_BIND_PORT:-8443}"
export JOI_OLLAMA_URL="${JOI_OLLAMA_URL:-http://localhost:11434}"
export JOI_OLLAMA_MODEL="${JOI_OLLAMA_MODEL:-llama3}"
export JOI_MESH_URL="${JOI_MESH_URL:-http://mesh:8444}"
export JOI_LOG_LEVEL="${JOI_LOG_LEVEL:-INFO}"

python3 -m api.server
