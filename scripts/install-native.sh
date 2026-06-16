#!/usr/bin/env bash
# install-native.sh — install context-engine as a native launchd service
#
# Usage:     bash scripts/install-native.sh
# Uninstall: bash scripts/install-native.sh --uninstall
#
# Env vars are baked into the plist at install time; launchd does not source
# files at runtime. After editing .env, re-run this script to apply changes.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PLIST_LABEL="life.ascendvent.context-manager"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
LOG_FILE="$HOME/Library/Logs/context-manager.log"
VENV="$ROOT/context-engine/.venv"
PORT=8088

# ── uninstall ──────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--uninstall" ]]; then
  echo "── uninstalling context-engine ─────────────────────────"
  launchctl unload "$PLIST_PATH" 2>/dev/null && echo "✓ unloaded" || echo "  (was not loaded)"
  rm -f "$PLIST_PATH" && echo "✓ plist removed"
  echo "  venv and logs left in place. Remove manually if wanted:"
  echo "    rm -rf $VENV"
  echo "    rm -f $LOG_FILE"
  exit 0
fi

echo "── context-engine native install ───────────────────────"

# ── python ─────────────────────────────────────────────────────────────────────
PYTHON=""
for candidate in python3.13 python3.12 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    path="$(command -v "$candidate")"
    pyver="$("$path" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")"
    if [[ "$pyver" == "3.12" || "$pyver" == "3.13" ]]; then
      PYTHON="$path"
      break
    fi
  fi
done
if [[ -z "$PYTHON" ]]; then
  echo "✗ Python 3.12 or 3.13 not found. Install via: brew install python@3.13"
  exit 1
fi
PYVER=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "✓ python $PYVER at $PYTHON"

# ── venv + deps ────────────────────────────────────────────────────────────────
if [[ -d "$VENV" ]]; then
  VENV_PYVER=$("$VENV/bin/python" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)
  if [[ "$VENV_PYVER" != "$PYVER" ]]; then
    echo "→ recreating venv for python $PYVER (was ${VENV_PYVER:-unknown})"
    rm -rf "$VENV"
  fi
fi

if [[ ! -d "$VENV" ]]; then
  echo "→ creating venv at context-engine/.venv"
  "$PYTHON" -m venv --system-site-packages "$VENV"
fi

echo "→ installing requirements"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$ROOT/context-engine/requirements.txt"
echo "✓ dependencies installed"

# ── .env ───────────────────────────────────────────────────────────────────────
if [[ ! -f "$ROOT/.env" ]]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo ""
  echo "  ⚠  Created .env from .env.example."
  echo "  Edit it now, then re-run this script:"
  echo "    $ROOT/.env"
  echo ""
  echo "  Required configuration:"
  echo "    OLLAMA_HOST=http://localhost:11434"
  echo "    REPO_ROOT=$HOME/Repos"
  echo "    SUPABASE_URL=<your Supabase URL>"
  echo "    SUPABASE_SERVICE_ROLE_KEY=<your key>"
  exit 0
fi

# ── logs ───────────────────────────────────────────────────────────────────────
mkdir -p "$(dirname "$LOG_FILE")"
echo "✓ log file: $LOG_FILE"

# ── unload existing ────────────────────────────────────────────────────────────
if launchctl list "$PLIST_LABEL" &>/dev/null; then
  echo "→ unloading existing service"
  launchctl unload "$PLIST_PATH" 2>/dev/null || true
fi

# ── build plist — env vars baked in from .env at install time ─────────────────
# launchd does not source files at runtime; vars must be explicit in the plist.
# Re-run this script after editing .env to pick up changes.

env_xml=""
has_repo_root=0
while IFS= read -r line || [[ -n "$line" ]]; do
  # skip blanks and comments
  [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
  # skip lines without =
  [[ "$line" != *"="* ]] && continue
  key="${line%%=*}"
  val="${line#*=}"
  # skip placeholder secrets
  [[ "$val" == "your-"* ]] && continue
  if [[ "$key" == "REPO_ROOT" ]]; then
    has_repo_root=1
    if [[ "$val" == "/repo" ]]; then
      val="$HOME/Repos"
    fi
  fi
  env_xml+="    <key>${key}</key>"$'\n'
  env_xml+="    <string>${val}</string>"$'\n'
done < "$ROOT/.env"
if [[ "$has_repo_root" -eq 0 ]]; then
  env_xml+="    <key>REPO_ROOT</key>"$'\n'
  env_xml+="    <string>${HOME}/Repos</string>"$'\n'
fi
echo "✓ environment loaded from .env"

cat > "$PLIST_PATH" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${PLIST_LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>${VENV}/bin/python</string>
    <string>-m</string>
    <string>uvicorn</string>
    <string>app.main:app</string>
    <string>--host</string>
    <string>0.0.0.0</string>
    <string>--port</string>
    <string>${PORT}</string>
    <string>--workers</string>
    <string>1</string>
    <string>--no-access-log</string>
  </array>

  <key>WorkingDirectory</key>
  <string>${ROOT}/context-engine</string>

  <key>EnvironmentVariables</key>
  <dict>
${env_xml}  </dict>

  <key>StandardOutPath</key>
  <string>${LOG_FILE}</string>

  <key>StandardErrorPath</key>
  <string>${LOG_FILE}</string>

  <key>KeepAlive</key>
  <true/>

  <key>RunAtLoad</key>
  <true/>

  <key>ThrottleInterval</key>
  <integer>10</integer>
</dict>
</plist>
PLIST

echo "✓ plist written: $PLIST_PATH"

# ── load ───────────────────────────────────────────────────────────────────────
launchctl load "$PLIST_PATH"
echo "✓ service loaded"

# ── wait for health ────────────────────────────────────────────────────────────
echo "→ waiting for health check (up to 20s)..."
for i in $(seq 1 20); do
  if curl -sf "http://localhost:${PORT}/healthcheck" > /dev/null 2>&1; then
    echo "✓ context-engine is up at http://localhost:${PORT}"
    break
  fi
  if [[ $i -eq 20 ]]; then
    echo "✗ timed out — check logs:"
    echo "    tail -f $LOG_FILE"
    exit 1
  fi
  sleep 1
done

echo ""
echo "── done ────────────────────────────────────────────────"
echo "  API:       http://localhost:${PORT}"
echo "  Logs:      tail -f $LOG_FILE"
echo "  Restart:   launchctl kickstart -k gui/$(id -u)/${PLIST_LABEL}"
echo "  Stop:      launchctl unload $PLIST_PATH"
echo "  Uninstall: bash scripts/install-native.sh --uninstall"
echo ""
echo "  ⚠  Env vars are baked into the plist at install time."
echo "  After editing .env, re-run this script to apply changes:"
echo "    bash scripts/install-native.sh"
echo "────────────────────────────────────────────────────────"
