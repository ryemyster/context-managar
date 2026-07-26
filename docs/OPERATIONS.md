# Operations Guide — context-engine

## Architecture

Two launchd services, one Python process each:

| Service | Plist | Port | Purpose |
|---------|-------|------|---------|
| REST API | `life.ascendvent.context-manager.plist` | `0.0.0.0:8088` | REST endpoints and agent runtime |
| MCP transport | `life.ascendvent.context-engine-mcp.plist` | `127.0.0.1:8089/mcp` | Streamable HTTP MCP adapter |

Both auto-start on login and restart on crash (`KeepAlive=true`). No Docker. No rebuild step — it's plain Python.

---

## Health checks

```bash
# Quick liveness check
curl -s http://localhost:8088/healthcheck

# Full health — configured inference models + Supabase
curl -s http://localhost:8088/health | python3 -m json.tool
```

A healthy `/health` response shows `status: ok` for each model and Supabase.
Check the configured generation provider's model inventory; check local
embeddings separately with `ollama list`.

---

## Service management

### Plist files

Two plist files control the two services:

| Plist | Service |
|-------|---------|
| `~/Library/LaunchAgents/life.ascendvent.context-manager.plist` | REST API on `:8088` |
| `~/Library/LaunchAgents/life.ascendvent.context-engine-mcp.plist` | MCP transport on `:8089/mcp` |

Both are installed in `~/Library/LaunchAgents/` — user-level agents, auto-started on login, restarted on crash.

---

### Day-to-day operations

```bash
# Status — col 1 = PID (empty = stopped), col 2 = last exit code, col 3 = label
launchctl list | grep context

# Stop REST API
launchctl unload ~/Library/LaunchAgents/life.ascendvent.context-manager.plist

# Start REST API
launchctl load ~/Library/LaunchAgents/life.ascendvent.context-manager.plist

# Reinstall/restart after Python or .env changes
bash scripts/install-native.sh

# Install or restart MCP transport (handles both unload/copy/load in one step)
bash scripts/install-mcp.sh
```

**Never assume a code change works by reading it. Always curl the endpoint after restarting.**

---

### Deploying for the first time

1. Create `.env` from `.env.example` and set repository, artifact, inference,
   and Supabase values.
2. Generate and load both launchd services:
   ```bash
   bash scripts/install-native.sh
   bash scripts/install-mcp.sh
   ```
3. Confirm both are running:
   ```bash
   launchctl list | grep context
   curl -s http://localhost:8088/healthcheck
   ```

---

### Updating launchd configuration

The installers generate the plist files. Do not copy nonexistent static plist
templates from the repository. Rerun the appropriate installer:

```bash
bash scripts/install-native.sh
bash scripts/install-mcp.sh
```

---

### Changing an environment variable

The application does not load `.env` at runtime. `install-native.sh` parses
`.env` and bakes values into the generated launchd plist.

```bash
# Edit the repository-root .env, then regenerate/restart
nano .env
bash scripts/install-native.sh
```

Common vars to tune at runtime: `LOG_LEVEL`, `SLOW_REQUEST_MS`, `ARTIFACTS_MAX_MB`.

`mcp_server.py` supports `CONTEXT_ENGINE_API_KEY`, but
`scripts/install-mcp.sh` does not currently add it to the generated MCP plist.
Enabling REST authentication will break the persistent MCP adapter until that
installer gap is fixed or the plist is modified explicitly.

---

### Undeploying (remove a service permanently)

```bash
# Stop and deregister (survives reboot — will NOT restart on login)
launchctl unload ~/Library/LaunchAgents/life.ascendvent.context-manager.plist
rm ~/Library/LaunchAgents/life.ascendvent.context-manager.plist
```

Do the same for the MCP transport if removing both services.

---

### Disabling without removing (temporarily stop auto-start)

```bash
# Unload but keep the plist — re-load manually when needed
launchctl unload ~/Library/LaunchAgents/life.ascendvent.context-manager.plist
```

The service will not restart on login until you `launchctl load` it again. The plist stays in place.

---

## Logs

Two services, two log files:

| Service | Log file |
|---|---|
| REST API (`:8088`) | `~/Library/Logs/context-manager.log` |
| MCP transport (`:8089/mcp`) | `~/Library/Logs/context-engine-mcp.log` |

```bash
# Follow REST API live
tail -f ~/Library/Logs/context-manager.log

# Follow MCP transport live
tail -f ~/Library/Logs/context-engine-mcp.log

# Last 50 lines (REST)
tail -50 ~/Library/Logs/context-manager.log

# Errors and warnings only
grep -i "error\|warn\|critical" ~/Library/Logs/context-manager.log | tail -30

# Watch a specific endpoint
tail -f ~/Library/Logs/context-manager.log | grep "/find\|/context\|/scan"

# Confirm clean startup (config loaded correctly)
grep "context-engine starting\|context-engine ready\|repo_root\|supabase=" \
  ~/Library/Logs/context-manager.log | tail -10
```

> **If `tail -f` reports "No such file or directory":** newsyslog rotated the log but launchd cannot reopen its stdout/stderr file descriptor. Restart the service to recreate the file:
> ```bash
> launchctl unload ~/Library/LaunchAgents/life.ascendvent.context-manager.plist
> launchctl load  ~/Library/LaunchAgents/life.ascendvent.context-manager.plist
> ```

### Log format

```
YYYY-MM-DD HH:MM:SS [LEVEL   ] message
```

Key lines:

| Line | Meaning |
|------|---------|
| `context-engine ready on :8088` | Clean startup |
| `store_artifact done` | Artifact indexed to Supabase |
| `store_artifact failed` | Background indexing error (non-fatal) |
| `SLOW` | Request exceeded `SLOW_REQUEST_MS` threshold (default 5 s) |

### Log rotation

macOS **newsyslog** manages rotation automatically. Config: `scripts/context-manager.newsyslog.conf`.

Current settings:
- Rotates when the log hits **10 MB**
- Keeps **5 compressed archives** (`.gz`)
- Total max disk: ~60 MB

```bash
# Install once (requires sudo) — already done on this machine
sudo cp scripts/context-manager.newsyslog.conf /etc/newsyslog.d/context-manager.conf

# Verify newsyslog picked it up ("skipping" = below threshold, not an error)
sudo newsyslog -nv 2>&1 | grep context-manager

# Force rotation now regardless of size
sudo newsyslog -F /Users/rmcdonald/Library/Logs/context-manager.log
```

#### Retention under DEBUG / TRACE

At `INFO`, 10 MB lasts days. At `DEBUG`, it fills in hours. At `TRACE` (raw prompt/response content), it fills in minutes under load.

With 5 archives at 10 MB each you get roughly **50 MB of rolling history**. When a new rotation happens the oldest archive is deleted automatically — you cannot run out of disk, but you also cannot look back further than those 5 rotations.

**If you're leaving DEBUG on for a long debugging session**, increase the archive count so you don't lose early history:

```bash
# Open the newsyslog config
nano scripts/context-manager.newsyslog.conf
# Find the count column (currently "5") and bump to e.g. 15
# Then reinstall:
sudo cp scripts/context-manager.newsyslog.conf /etc/newsyslog.d/context-manager.conf
```

Alternatively increase the rotation threshold (size column, in KB):

```
# Current: 10240 (10 MB)
# Change to: 51200 (50 MB) for less-frequent but larger archives
```

Remember to set it back after your debug session — TRACE at 50 MB/rotation can accumulate fast.

**Recommended for multi-day DEBUG sessions:** archives = 15, size = 10240 (150 MB max, gives ~15 rotation windows at INFO cadence).

---

## Inference routing

| Role | Env var | Used by |
|------|---------|---------|
| Reasoning | `INFERENCE_REASONING_MODEL` | `/diff-summary` |
| Agent | `INFERENCE_AGENT_MODEL` | `/agents/run` structured selection and answer generation |
| Selection fallback / verification | `INFERENCE_SELECTION_MODEL` / `INFERENCE_VERIFICATION_MODEL` | `/agents/run` fallback tool calling and evidence verification |
| Fast | `INFERENCE_FAST_MODEL` | `/context`, `/draft`, `/scaffold`, `/scan`, `/find`, `/summarize` |
| Embedding | `INFERENCE_EMBEDDING_MODEL` | `/index`, `/vector-search`, `/context` |

For cloud generation, set `INFERENCE_GENERATION_PROVIDER=openai_compatible`.
Keep `INFERENCE_EMBEDDING_PROVIDER=ollama` and
`INFERENCE_EMBEDDING_MODEL=nomic-embed-text` to preserve the existing vector
corpus. See `docs/inference-service.md`.

---

## Artifacts — output store

Inference and agent workflows that produce artifacts write to two places:

1. **Supabase cloud** — embedded + upserted as vector chunks (primary, searchable across sessions)
2. **Disk backup** — `~/Library/Application Support/context-store/artifacts/` (crash recovery only)

LRU eviction kicks in when the artifacts dir exceeds `ARTIFACTS_MAX_MB` (default 50 MB) — oldest files deleted on each write. Disk copies are expendable; Supabase is the source of truth.

```bash
# Check disk usage
du -sh ~/Library/Application\ Support/context-store/artifacts/

# List by age (oldest first)
ls -lt ~/Library/Application\ Support/context-store/artifacts/ | tail -20

# Manual wipe (safe — everything is in Supabase)
rm ~/Library/Application\ Support/context-store/artifacts/*.md
```

---

## Debugging runbook

### Service won't start

```bash
# Check last exit code (non-zero = crash on startup)
launchctl list | grep context-manager

# Read the last startup attempt
grep "context-engine starting\|Traceback\|Error\|error" \
  ~/Library/Logs/context-manager.log | tail -20
```

Common causes: missing `.env` value, inference provider unavailable, local
embedding Ollama not running, or port already in use.

### Endpoint returns 500

```bash
# Tail logs while reproducing
tail -f ~/Library/Logs/context-manager.log

# Then hit the endpoint in another terminal
curl -s -X POST http://localhost:8088/<endpoint> \
  -H "Content-Type: application/json" \
  -d '{"field": "value"}' | python3 -m json.tool
```

### Slow responses

Look for `SLOW` lines in the log — they include the endpoint and elapsed ms.
Common causes: provider cold starts, a local embedding model swap, a large file
scan, or Supabase network latency.

### MCP tools not responding

```bash
# Check the MCP transport service
launchctl list | grep context-engine-mcp

# Reinstall and restart
bash scripts/install-mcp.sh
```

### Supabase indexing failures (`store_artifact failed`)

These are non-fatal — the REST response still succeeds. Check Supabase connectivity:

```bash
supabase db query --linked "SELECT count(*) FROM code_embeddings;"
```

If the query fails, check `SUPABASE_URL` and
`SUPABASE_SERVICE_ROLE_KEY` in `.env`.

---

## Database

Supabase cloud project: `rwtaxwtbwtyxcdlkozod.supabase.co`. No local Supabase instance.

**Always use `db query --linked -f` for migrations — never `db push`.** The cloud project is shared with checkin-ascendvent and has migrations this repo doesn't track.

```bash
# Apply a migration
supabase migration new <descriptive-name>
# write SQL in supabase/migrations/<timestamp>_<name>.sql
supabase db query --linked -f supabase/migrations/<your-file>.sql

# Verify
supabase db query --linked "SELECT count(*) FROM code_embeddings;"
```

### Wipe and re-seed vector data

Only needed after a schema change or to clear stale embeddings.

```bash
supabase db query --linked "TRUNCATE code_embeddings;"
curl -s -X POST http://localhost:8088/index \
  -H "Content-Type: application/json" \
  -d '{"paths": ["ascendvent/checkin-ascendvent/app", "ascendvent/founderos/src"], "force": true}'
```
