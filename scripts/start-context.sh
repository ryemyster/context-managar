#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$SCRIPT_DIR/.."
BASE_URL="http://localhost:8088"

echo "── context-engine startup ──────────────────────────────"

# Create output folder
mkdir -p "$ROOT/ai-context"

# Copy .env if missing
if [ ! -f "$ROOT/.env" ]; then
  echo "→ No .env found — copying .env.example"
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "  ⚠  Edit .env and set:"
  echo "     REPO_PATH=<your repo>"
  echo "     SUPABASE_SERVICE_ROLE_KEY=<your key>"
  echo ""
fi

# Check founderos-ollama
if ! docker inspect founderos-ollama > /dev/null 2>&1; then
  echo "✗ founderos-ollama is not running. Start founderos first."
  exit 1
fi
echo "✓ founderos-ollama running"

# Check models
MODELS=$(curl -s http://localhost:11434/api/tags | python3 -c "import json,sys; [print(m['name']) for m in json.load(sys.stdin)['models']]" 2>/dev/null || echo "")

if echo "$MODELS" | grep -q "qwen2.5-coder:3b"; then
  echo "✓ qwen2.5-coder:3b available (code model)"
else
  echo "⚠  qwen2.5-coder:3b not found. Run: bash scripts/pull-model.sh"
fi

if echo "$MODELS" | grep -q "qwen3"; then
  echo "✓ qwen3.5:9b available (reasoning model)"
else
  echo "⚠  qwen3.5:9b not found. Run: ollama pull qwen3.5:9b"
fi

if echo "$MODELS" | grep -q "nomic-embed-text"; then
  echo "✓ nomic-embed-text available (embedding model)"
else
  echo "⚠  nomic-embed-text not found. Run: bash scripts/pull-model.sh"
fi

# Check Supabase network
if ! docker network inspect supabase_network_checkin-ascendvent > /dev/null 2>&1; then
  echo "⚠  supabase_network_checkin-ascendvent not found"
  echo "   Supabase features will be unavailable"
else
  echo "✓ supabase network found"
fi

echo ""
echo "→ Building and starting context-engine..."
cd "$ROOT"
docker compose up -d --build

echo ""
echo "→ Waiting for health check (up to 40s)..."
for i in $(seq 1 40); do
  STATUS=$(curl -sf "$BASE_URL/health" 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print('ok' if d.get('status')=='ok' else 'not_ok')" 2>/dev/null || echo "")
  if [ "$STATUS" = "ok" ]; then
    echo "✓ context-engine is healthy"
    break
  fi
  if [ "$i" -eq 40 ]; then
    echo "✗ Timed out. Check: docker logs context-engine"
    exit 1
  fi
  sleep 1
done

echo ""
echo "── Health status ──────────────────────────────────────"
curl -s "$BASE_URL/health" | python3 -c "
import json, sys
d = json.load(sys.stdin)
for k, v in d.items():
    print(f'  {k}: {v}')
" 2>/dev/null || true

echo ""
echo "── Ready ───────────────────────────────────────────────"
echo "  API:     $BASE_URL"
echo "  Docs:    $BASE_URL/docs"
echo "  Output:  ./ai-context/"
echo ""
echo "  Quick start:"
echo "    bash scripts/routes.sh"
echo "    bash scripts/scan.sh src/app"
echo "    bash scripts/context.sh 'Add plan enforcement to checkins'"
echo "────────────────────────────────────────────────────────"
