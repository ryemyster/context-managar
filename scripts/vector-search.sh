#!/usr/bin/env bash
# Usage: bash scripts/vector-search.sh "stripe subscription enforcement" [limit]

QUERY="${1:?Usage: bash scripts/vector-search.sh 'query' [limit]}"
LIMIT="${2:-8}"
BASE_URL="${CONTEXT_ENGINE_URL:-http://localhost:8088}"

echo "→ Vector search: \"$QUERY\" (limit: $LIMIT)"
echo "  Requires: Supabase vector table + nomic-embed-text"
echo ""

curl -s -X POST "$BASE_URL/vector-search" \
  -H "Content-Type: application/json" \
  -d "{\"query\": $(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$QUERY"), \"limit\": $LIMIT}" \
  | python3 -m json.tool
