#!/usr/bin/env bash
# Usage: bash scripts/summarize.sh src/app/api/checkins/route.ts

FILE="${1:?Usage: bash scripts/summarize.sh path/to/file.ts}"
BASE_URL="${CONTEXT_ENGINE_URL:-http://localhost:8088}"

echo "→ Summarizing: $FILE ..."
echo ""

curl -s -X POST "$BASE_URL/summarize" \
  -H "Content-Type: application/json" \
  -d "{\"file\": \"$FILE\"}" \
  | python3 -m json.tool
