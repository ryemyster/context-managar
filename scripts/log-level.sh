#!/usr/bin/env bash
# Usage: ./scripts/log-level.sh [LEVEL]
#   No arg  — print current level
#   LEVEL   — set to TRACE | DEBUG | INFO | WARNING | ERROR
set -euo pipefail

BASE="http://localhost:8088"

if [[ $# -eq 0 ]]; then
    curl -sf "$BASE/log-level" | python3 -m json.tool
else
    LEVEL=$(echo "$1" | tr '[:lower:]' '[:upper:]')
    curl -sf -X POST "$BASE/log-level" \
        -H "Content-Type: application/json" \
        -d "{\"level\": \"$LEVEL\"}" | python3 -m json.tool
fi
