#!/usr/bin/env bash
# Usage: bash scripts/scan.sh [path]
# Example: bash scripts/scan.sh src/app/api
# Omit path to scan the repo root.

PATH_ARG="${1:-}"
BASE_URL="${CONTEXT_ENGINE_URL:-http://localhost:8088}"

echo "→ Scanning: ${PATH_ARG:-/} ..."
echo "  (model call — may take 30-90s on first request)"
echo ""

curl -s -X POST "$BASE_URL/scan" \
  -H "Content-Type: application/json" \
  -d "{\"path\": \"$PATH_ARG\"}" \
  | python3 -m json.tool

echo ""
echo "Context written to ./ai-context/ — see written_to above."
