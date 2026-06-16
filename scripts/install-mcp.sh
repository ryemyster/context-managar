#!/usr/bin/env bash
# Install the persistent Context Engine MCP Streamable HTTP launchd service.
#
# Usage:     bash scripts/install-mcp.sh
# Uninstall: bash scripts/install-mcp.sh --uninstall
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

LABEL="life.ascendvent.context-engine-mcp"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG="$HOME/Library/Logs/context-engine-mcp.log"
VENV="$ROOT/context-engine/.mcp-venv"
PYTHON="$VENV/bin/python3"
PYTHON_BIN=""
HOST="${CONTEXT_ENGINE_MCP_HOST:-127.0.0.1}"
PORT="${CONTEXT_ENGINE_MCP_PORT:-8089}"
MCP_URL="http://${HOST}:${PORT}/mcp"
OUTPUT_DIR="${OUTPUT_DIR:-${HOME}/Library/Application Support/context-store/artifacts}"
MCP_INLINE_LIMIT="${MCP_INLINE_LIMIT:-1000}"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed ${LABEL}. The isolated virtualenv and log were left in place."
  exit 0
fi

for candidate in python3.13 python3.12 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    path="$(command -v "$candidate")"
    pyver="$("$path" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")"
    if [[ "$pyver" == "3.12" || "$pyver" == "3.13" ]]; then
      PYTHON_BIN="$path"
      break
    fi
  fi
done
if [[ -z "$PYTHON_BIN" ]]; then
  echo "Python 3.12 or 3.13 not found. Install via: brew install python@3.13" >&2
  exit 1
fi

PYVER=$("$PYTHON_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
if [[ -d "$VENV" ]]; then
  VENV_PYVER=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)
  if [[ "$VENV_PYVER" != "$PYVER" ]]; then
    rm -rf "$VENV"
  fi
fi

if [[ ! -d "$VENV" ]]; then
  "$PYTHON_BIN" -m venv "$VENV"
fi

"$PYTHON" -m pip install -q -r "$ROOT/context-engine/requirements-mcp.txt"

mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON}</string>
    <string>${ROOT}/context-engine/mcp_http_server.py</string>
  </array>

  <key>WorkingDirectory</key>
  <string>${ROOT}/context-engine</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>${HOME}</string>
    <key>PATH</key>
    <string>/usr/local/bin:/usr/bin:/bin</string>
    <key>CONTEXT_ENGINE_URL</key>
    <string>${CONTEXT_ENGINE_URL:-http://127.0.0.1:8088}</string>
    <key>CONTEXT_ENGINE_MCP_HOST</key>
    <string>${HOST}</string>
    <key>CONTEXT_ENGINE_MCP_PORT</key>
    <string>${PORT}</string>
    <key>CONTEXT_ENGINE_MCP_REQUEST_TIMEOUT</key>
    <string>${CONTEXT_ENGINE_MCP_REQUEST_TIMEOUT:-120}</string>
    <key>CONTEXT_ENGINE_MCP_RUN_TIMEOUT</key>
    <string>${CONTEXT_ENGINE_MCP_RUN_TIMEOUT:-900}</string>
    <key>CONTEXT_ENGINE_MCP_POLL_INTERVAL</key>
    <string>${CONTEXT_ENGINE_MCP_POLL_INTERVAL:-1}</string>
    <key>OUTPUT_DIR</key>
    <string>${OUTPUT_DIR}</string>
    <key>MCP_INLINE_LIMIT</key>
    <string>${MCP_INLINE_LIMIT}</string>
  </dict>

  <key>StandardOutPath</key>
  <string>${LOG}</string>
  <key>StandardErrorPath</key>
  <string>${LOG}</string>
  <key>KeepAlive</key>
  <true/>
  <key>RunAtLoad</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
</dict>
</plist>
PLIST

plutil -lint "$PLIST"
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/${LABEL}"

for _ in $(seq 1 20); do
  if curl -sS --max-time 2 \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"install-check","version":"1.0"}}}' \
    "$MCP_URL" | grep -q '"serverInfo"'; then
    echo "Context Engine MCP is running at ${MCP_URL}"
    echo "Logs: ${LOG}"
    exit 0
  fi
  sleep 1
done

echo "MCP service did not become ready. Check ${LOG}" >&2
exit 1
