#!/usr/bin/env bash
# Index code into Supabase vector store.
# Run before a coding session. Uses nomic-embed-text only (no qwen).
#
# Usage:
#   bash scripts/index.sh                           # index src/app,src/lib,supabase
#   bash scripts/index.sh "src/app,src/lib"         # custom paths
#   bash scripts/index.sh "src/app" force           # force re-index

PATHS_RAW="${1:-src/app,src/lib,supabase}"
FORCE="${2:-false}"
BASE_URL="${CONTEXT_ENGINE_URL:-http://localhost:8088}"

[ "$FORCE" = "force" ] && FORCE_BOOL="true" || FORCE_BOOL="false"

PATHS_JSON=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1].split(',')))" "$PATHS_RAW")

echo "── Indexing ────────────────────────────────────────────"
echo "  Paths:  $PATHS_RAW"
echo "  Force:  $FORCE_BOOL"
echo ""
echo "  ⚠  CPU-only embedding: ~1-2s per chunk"
echo "     80 files ≈ 2-5 minutes"
echo "     Run before your coding session, not during"
echo ""

curl -s -X POST "$BASE_URL/index" \
  -H "Content-Type: application/json" \
  -d "{\"paths\": $PATHS_JSON, \"force\": $FORCE_BOOL}" \
  | python3 -m json.tool

echo ""
echo "Index report: ./ai-context/index-report.md"
