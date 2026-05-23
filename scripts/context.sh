#!/usr/bin/env bash
# Build a context bundle for a specific Claude task.
#
# Usage:
#   bash scripts/context.sh "Add plan enforcement to check-in generation"
#   bash scripts/context.sh "Add plan enforcement" "src/app,src/lib" "auth,stripe,checkins"
#
# Args:
#   $1 — task description (required)
#   $2 — comma-separated paths to scan (default: "src/app,src/lib,supabase")
#   $3 — comma-separated focus terms (default: derived from task words)

TASK="${1:?Usage: bash scripts/context.sh 'task description' [paths] [focus]}"
PATHS_RAW="${2:-src/app,src/lib,supabase}"
FOCUS_RAW="${3:-}"
BASE_URL="${CONTEXT_ENGINE_URL:-http://localhost:8088}"

# Convert comma-separated to JSON arrays
PATHS_JSON=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1].split(',')))" "$PATHS_RAW")
if [ -n "$FOCUS_RAW" ]; then
  FOCUS_JSON=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1].split(',')))" "$FOCUS_RAW")
else
  # Default focus: significant words from task
  FOCUS_JSON=$(python3 -c "
import json, sys
words = sys.argv[1].lower().split()
stop = {'add','the','a','an','to','for','in','of','and','or','with','on','at','by','is','are','was','be','this','that','it','from'}
focus = [w for w in words if w not in stop and len(w) > 3][:6]
print(json.dumps(focus))
" "$TASK")
fi

echo "── Context Bundle ──────────────────────────────────────"
echo "  Task:   $TASK"
echo "  Paths:  $PATHS_RAW"
echo "  Focus:  $FOCUS_JSON"
echo ""
echo "  Running: scan → grep → vector search → synthesis"
echo "  (may take 60-120s — model calls involved)"
echo ""

curl -s -X POST "$BASE_URL/context" \
  -H "Content-Type: application/json" \
  -d "{
    \"task\": $(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$TASK"),
    \"paths\": $PATHS_JSON,
    \"focus\": $FOCUS_JSON
  }" \
  | python3 -m json.tool

echo ""
echo "Context bundle: ./ai-context/context-bundle.md"
echo ""
echo "── Next step ───────────────────────────────────────────"
echo "  Tell Claude:"
echo "  'Read ./ai-context/context-bundle.md then implement: $TASK'"
echo "────────────────────────────────────────────────────────"
