#!/usr/bin/env bash
set -euo pipefail

# Pulls qwen2.5-coder:3b into the existing founderos-ollama container.
# Calls localhost:11434 (host-exposed port) — does NOT need to be inside Docker.

OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
MODEL="${1:-qwen2.5-coder:3b}"

echo "── Ollama Model Pull ────────────────────────────────────"
echo "  Ollama: $OLLAMA_URL"
echo "  Model:  $MODEL"
echo ""

# Check Ollama is reachable
if ! curl -sf "$OLLAMA_URL/api/tags" > /dev/null 2>&1; then
  echo "✗ Cannot reach Ollama at $OLLAMA_URL"
  echo "  Ensure founderos-ollama is running and port 11434 is exposed."
  exit 1
fi

# Check if already pulled
ALREADY=$(curl -s "$OLLAMA_URL/api/tags" | grep -c "\"$MODEL\"" || true)
if [ "$ALREADY" -gt 0 ]; then
  echo "✓ $MODEL is already available. Nothing to do."
  exit 0
fi

echo "→ Pulling $MODEL (this will take a few minutes on first run)..."
echo "  Model size: ~1.9 GB"
echo ""

# Stream the pull progress
curl -s -X POST "$OLLAMA_URL/api/pull" \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"$MODEL\"}" \
  --no-buffer | while IFS= read -r line; do
    STATUS=$(echo "$line" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status',''))" 2>/dev/null || true)
    TOTAL=$(echo "$line" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('total',''))" 2>/dev/null || true)
    COMPLETED=$(echo "$line" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('completed',''))" 2>/dev/null || true)
    if [ -n "$TOTAL" ] && [ -n "$COMPLETED" ] && [ "$TOTAL" -gt 0 ] 2>/dev/null; then
      PCT=$(python3 -c "print(f'{int($COMPLETED/$TOTAL*100)}%')" 2>/dev/null || true)
      printf "\r  %-40s %s     " "$STATUS" "$PCT"
    elif [ -n "$STATUS" ]; then
      echo "  $STATUS"
    fi
  done

echo ""
echo ""

# Verify
FOUND=$(curl -s "$OLLAMA_URL/api/tags" | grep -c "$MODEL" || true)
if [ "$FOUND" -gt 0 ]; then
  echo "✓ $MODEL is ready"
else
  echo "✗ Pull may have failed. Check: curl $OLLAMA_URL/api/tags"
  exit 1
fi

echo ""
echo "  Memory tip: qwen2.5-coder:3b uses ~2.2 GB when loaded."
echo "  It will auto-unload after OLLAMA_KEEP_ALIVE idle time."
echo "  See docs/ollama-optimization.md for tuning."
echo "────────────────────────────────────────────────────────"
