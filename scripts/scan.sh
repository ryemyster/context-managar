#!/usr/bin/env bash
# Usage: bash scripts/scan.sh owner/repo/path [summary|full] [max_results]
# Example: bash scripts/scan.sh ryemyster/context-manager/context-engine/app
# Default mode is context-safe summary references. Pass "full" only when needed.

PATH_ARG="${1:?Usage: bash scripts/scan.sh owner/repo/path [summary|full] [max_results]}"
DETAIL="${2:-summary}"
MAX_RESULTS="${3:-20}"
BASE_URL="${CONTEXT_ENGINE_URL:-http://localhost:8088}"

echo "→ Scanning: $PATH_ARG ..."
echo "  (model call — may take 30-90s on first request)"
echo ""

PAYLOAD=$(python3 -c 'import json,sys
path, detail, max_results = sys.argv[1], sys.argv[2], int(sys.argv[3])
payload = {"path": path}
if detail == "full":
    payload["detail"] = "full"
else:
    payload.update({"mode": "context_safe", "max_results": max_results})
print(json.dumps(payload))' "$PATH_ARG" "$DETAIL" "$MAX_RESULTS")

curl -s -X POST "$BASE_URL/scan" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD" \
  | python3 -m json.tool

echo ""
echo "Artifact path is in written_to. Use /read for selected file content."
