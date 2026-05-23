#!/usr/bin/env bash
# Extract all routes from the mounted repo.
# Output written to ./ai-context/routes.md

BASE_URL="${CONTEXT_ENGINE_URL:-http://localhost:8088}"

echo "→ Extracting routes..."
echo ""

curl -s -X POST "$BASE_URL/routes" \
  | python3 -m json.tool

echo ""
echo "Full route map: ./ai-context/routes.md"
