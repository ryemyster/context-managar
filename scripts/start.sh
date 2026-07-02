#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$SCRIPT_DIR/.."

echo "── local-model repo-analyzer ──────────────────────────"

# Create output folder if it doesn't exist
mkdir -p "$ROOT/ai-context"

# Copy .env.example if .env is missing
if [ ! -f "$ROOT/.env" ]; then
  echo "→ No .env found. Copying .env.example → .env"
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "  Edit .env and set REPO_PATH to the repo you want to analyze."
  echo ""
fi

# Check that founderos-ollama is running
if ! docker inspect founderos-ollama > /dev/null 2>&1; then
  echo "✗ founderos-ollama is not running."
  echo "  Start your founderos stack first, then re-run this script."
  exit 1
fi
echo "✓ founderos-ollama is running"

# Ollama remains required for the existing embedding corpus.
MODEL="${INFERENCE_EMBEDDING_MODEL:-${OLLAMA_EMBED_MODEL:-nomic-embed-text}}"
MODEL_FOUND=$(curl -s http://localhost:11434/api/tags | grep -c "$MODEL" || true)
if [ "$MODEL_FOUND" -eq 0 ]; then
  echo "⚠ Embedding model '$MODEL' not found in Ollama."
  echo "  Run: ollama pull $MODEL"
  echo "  Then re-run this script."
  exit 1
fi
echo "✓ Embedding model $MODEL is available"

echo ""
echo "→ Building and starting repo-analyzer..."
cd "$ROOT"
docker compose up -d --build

echo ""
echo "→ Waiting for health check (up to 30s)..."
for i in $(seq 1 30); do
  if curl -sf http://localhost:8088/health > /dev/null 2>&1; then
    echo "✓ repo-analyzer is healthy"
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "✗ Health check timed out. Check logs: docker logs repo-analyzer"
    exit 1
  fi
  sleep 1
done

echo ""
echo "── Ready ───────────────────────────────────────────────"
echo "  Health:  http://localhost:8088/health"
echo "  Docs:    http://localhost:8088/docs"
echo ""
echo "  Quick test:"
echo "    curl http://localhost:8088/health"
echo "    curl -X POST http://localhost:8088/routes"
echo "    bash scripts/scan.sh src/app/api"
echo ""
echo "  Output:  ./ai-context/"
echo "────────────────────────────────────────────────────────"
