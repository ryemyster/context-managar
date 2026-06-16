#!/usr/bin/env bash
# Usage: bash scripts/find.sh "query" owner/repo/path [summary|full] [max_results]
# Example: bash scripts/find.sh "auth middleware" ryemyster/context-manager/context-engine/app
# Default mode is context-safe summary references. Pass "full" only when needed.

QUERY="${1:?Usage: bash scripts/find.sh 'query' owner/repo/path [summary|full] [max_results]}"
PATH_ARG="${2:?Usage: bash scripts/find.sh 'query' owner/repo/path [summary|full] [max_results]}"
DETAIL="${3:-summary}"
MAX_RESULTS="${4:-20}"
BASE_URL="${CONTEXT_ENGINE_URL:-http://localhost:8088}"

echo "→ Finding: \"$QUERY\" in $PATH_ARG ..."
echo ""

PAYLOAD=$(python3 -c 'import json,sys
query, path, detail, max_results = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
payload = {"query": query, "path": path}
if detail == "full":
    payload["detail"] = "full"
else:
    payload.update({"mode": "context_safe", "max_results": max_results})
print(json.dumps(payload))' "$QUERY" "$PATH_ARG" "$DETAIL" "$MAX_RESULTS")

curl -s -X POST "$BASE_URL/find" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD" \
  | python3 -m json.tool
