#!/usr/bin/env bash
# Usage: bash scripts/vector-search.sh "stripe subscription enforcement" [limit] [summary|full]
# Default mode is context-safe summary references. Pass "full" only when needed.

QUERY="${1:?Usage: bash scripts/vector-search.sh 'query' [limit]}"
LIMIT="${2:-8}"
DETAIL="${3:-summary}"
BASE_URL="${CONTEXT_ENGINE_URL:-http://localhost:8088}"

echo "→ Vector search: \"$QUERY\" (limit: $LIMIT)"
echo "  Requires: Supabase vector table + nomic-embed-text"
echo ""

PAYLOAD=$(python3 -c 'import json,sys
query, limit, detail = sys.argv[1], int(sys.argv[2]), sys.argv[3]
payload = {"query": query, "limit": limit}
if detail == "full":
    payload["detail"] = "full"
else:
    payload.update({"mode": "context_safe", "max_results": limit})
print(json.dumps(payload))' "$QUERY" "$LIMIT" "$DETAIL")

curl -s -X POST "$BASE_URL/vector-search" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD" \
  | python3 -m json.tool
