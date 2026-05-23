#!/usr/bin/env bash
# Summarize a git diff.
#
# Usage:
#   git diff HEAD~1 | bash scripts/diff-summary.sh
#   bash scripts/diff-summary.sh < my.diff
#   git diff HEAD~1 | bash scripts/diff-summary.sh --pipe

BASE_URL="${CONTEXT_ENGINE_URL:-http://localhost:8088}"

echo "→ Reading diff from stdin..."
DIFF=$(cat)

if [ -z "$DIFF" ]; then
  echo "Error: no diff provided. Pipe a git diff:"
  echo "  git diff HEAD~1 | bash scripts/diff-summary.sh"
  exit 1
fi

echo "→ Summarizing diff (${#DIFF} chars)..."
echo ""

DIFF_JSON=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$DIFF")

curl -s -X POST "$BASE_URL/diff-summary" \
  -H "Content-Type: application/json" \
  -d "{\"diff\": $DIFF_JSON}" \
  | python3 -m json.tool
