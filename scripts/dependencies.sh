#!/usr/bin/env bash
# Usage: bash scripts/dependencies.sh [path]
# Example: bash scripts/dependencies.sh src/lib

PATH_ARG="${1:-.}"
BASE_URL="${CONTEXT_ENGINE_URL:-http://localhost:8088}"

echo "→ Mapping dependencies in: $PATH_ARG ..."
echo ""

curl -s -X POST "$BASE_URL/dependencies" \
  -H "Content-Type: application/json" \
  -d "{\"path\": \"$PATH_ARG\"}" \
  | python3 -m json.tool
