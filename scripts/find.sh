#!/usr/bin/env bash
# Usage: bash scripts/find.sh "query" [path]
# Example: bash scripts/find.sh "stripe subscription enforcement"
# Example: bash scripts/find.sh "auth middleware" src/app

QUERY="${1:?Usage: bash scripts/find.sh 'query' [path]}"
PATH_ARG="${2:-.}"
BASE_URL="${CONTEXT_ENGINE_URL:-http://localhost:8088}"

echo "→ Finding: \"$QUERY\" in $PATH_ARG ..."
echo ""

curl -s -X POST "$BASE_URL/find" \
  -H "Content-Type: application/json" \
  -d "{\"query\": \"$QUERY\", \"path\": \"$PATH_ARG\"}" \
  | python3 -m json.tool
