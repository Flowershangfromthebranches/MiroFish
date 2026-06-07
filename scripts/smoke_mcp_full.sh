#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$ROOT/backend"

cd "$BACKEND"
uv run python "$ROOT/scripts/smoke_mcp_full.py"
