# context-manager

This IS the context engine. Changes here affect every project that uses it as a scout.

## Project structure

```
context-engine/
  mcp_server.py        — shared thin MCP → REST adapter; no agent business logic
  mcp_http_server.py   — persistent Streamable HTTP transport on :8089/mcp
  requirements-mcp.txt — isolated MCP service dependencies
  app/
    main.py            — all routes + /setup doc (update /setup when adding endpoints)
    config.py          — env vars and constants — single source of truth
    ollama_client.py   — generate() · generate_reasoning() · embed()
    models.py          — Pydantic request/response models
    markdown_writer.py — write_*() per endpoint + LRU eviction (_evict_if_needed)
    supabase_vector.py — pgvector client + store_artifact() background indexer
    diff_reviewer.py   — /diff-summary logic
    context_builder.py — /context orchestration
    scanner.py         — /scan logic
    search_worker.py   — /find + grep logic
    route_extractor.py — /routes logic
    dependency_mapper.py — /dependencies logic
    repo_reader.py     — safe file reading + path guard (never bypass this)
  .venv/               — Python venv (gitignored)
  requirements.txt     — pinned dependencies
.env                   — env overrides (gitignored) — see .env.example
.env.example           — template for all configurable vars
supabase/migrations/   — SQL applied via db query --linked (not db push)
scripts/               — shell wrappers for each endpoint
  install-mcp.sh       — install/restart the MCP launchd service
config/                — Claude Code and Codex MCP configuration examples
docs/mcp-integration.md — MCP tools, installation, examples, and curl migration
```

## Three-model stack

| Model | Env var | Used by |
|-------|---------|---------|
| `qwen3.5:9b` | `OLLAMA_REASON_MODEL` | `/diff-summary` |
| `qwen2.5-coder:3b` | `OLLAMA_AGENT_MODEL` | `/agents/run` answer generation |
| `qwen2.5-coder:3b` | `OLLAMA_AGENT_SELECT_MODEL` / `OLLAMA_AGENT_VERIFY_MODEL` | `/agents/run` structured selection and verification |
| `qwen2.5-coder:3b` | `OLLAMA_MODEL` | `/context`, `/draft`, `/scaffold`, `/scan`, `/find`, `/summarize` |
| `nomic-embed-text` | `OLLAMA_EMBED_MODEL` | `/index`, `/vector-search`, `/context` (vector step) |

**Routing rule:** `/diff-summary` → `generate_reasoning()` (risk analysis is judgment). Everything else → `generate()` (code pattern matching) or `embed()`. `/context` uses code model for synthesis — relevance scoring is pattern matching, not reasoning.

## Service management (launchd)

Native Python/uvicorn process — no Docker. Plist:
`~/Library/LaunchAgents/life.ascendvent.context-manager.plist`

The MCP transport is a separate service:
`~/Library/LaunchAgents/life.ascendvent.context-engine-mcp.plist`

Auto-starts on login, restarts on crash (KeepAlive=true).

```bash
# Status — col 1 = PID, col 2 = last exit code, col 3 = label
launchctl list | grep context-manager

# Stop
launchctl unload ~/Library/LaunchAgents/life.ascendvent.context-manager.plist

# Start
launchctl load ~/Library/LaunchAgents/life.ascendvent.context-manager.plist

# Install/restart MCP transport
bash scripts/install-mcp.sh
```

## Logs

All stdout and stderr go to `~/Library/Logs/context-manager.log`.

```bash
# Follow live
tail -f ~/Library/Logs/context-manager.log

# Last 50 lines
tail -50 ~/Library/Logs/context-manager.log

# Filter errors only
grep -i "error\|warn\|critical" ~/Library/Logs/context-manager.log | tail -30

# Watch a specific endpoint
tail -f ~/Library/Logs/context-manager.log | grep "/find\|/context\|/scan"

# Check last startup (confirm config loaded correctly)
grep "context-engine starting\|context-engine ready\|repo_root\|supabase=" \
  ~/Library/Logs/context-manager.log | tail -10
```

Log format: `YYYY-MM-DD HH:MM:SS [LEVEL   ] message`
Key lines to look for:
- `context-engine ready on :8088` — clean startup
- `store_artifact done` — artifact indexed to Supabase successfully
- `store_artifact failed` — background indexing error (non-fatal)
- `SLOW` — request exceeded `SLOW_REQUEST_MS` threshold (default 5s)

### Log rotation (newsyslog)

The config file is `scripts/context-manager.newsyslog.conf`. It rotates at 10 MB, keeps 5 compressed archives (`.gz`).

**Install once (requires sudo):**
```bash
sudo cp scripts/context-manager.newsyslog.conf /etc/newsyslog.d/context-manager.conf
```

**Verify newsyslog picked it up:**
```bash
sudo newsyslog -nv 2>&1 | grep context-manager
# Expected output (healthy — "skipping" means below threshold, not an error):
# /Users/rmcdonald/Library/Logs/context-manager.log <5Z>: size (Kb): 12 [10240] --> skipping
```

**Force a manual rotation now (regardless of size):**
```bash
sudo newsyslog -F /Users/rmcdonald/Library/Logs/context-manager.log
```

macOS runs newsyslog automatically via `/System/Library/LaunchDaemons/com.apple.newsyslog.plist` — no further setup needed after the `cp`.

> Already installed on this machine at `/etc/newsyslog.d/context-manager.conf`.

## Dev workflow — redeploy after Python changes

No rebuild needed — it's plain Python. Just restart the service:

```bash
launchctl unload ~/Library/LaunchAgents/life.ascendvent.context-manager.plist
launchctl load  ~/Library/LaunchAgents/life.ascendvent.context-manager.plist
```

Confirm health after:
```bash
curl -s http://localhost:8088/health | python3 -m json.tool
```

Never assume a change works by reading the code. Always curl the endpoint.

## Dev workflow — running tests

```bash
# From the repo root
context-engine/.venv/bin/python3 -m pytest

# Verbose
context-engine/.venv/bin/python3 -m pytest -v

# Single test file
context-engine/.venv/bin/python3 -m pytest tests/test_agent_runner.py -v
```

Tests use `unittest.mock` — no Ollama or filesystem calls. Safe to run offline.
The venv has `include-system-site-packages = true` so system packages (httpx, pydantic, fastapi) are visible.

## Dev workflow — adding a Python dependency

```bash
# Install into the venv using python3 -m pip (the pip shebang may point to a stale path)
context-engine/.venv/bin/python3 -m pip install <package>

# Pin it in requirements.txt
echo "<package>==<version>" >> context-engine/requirements.txt

# Restart
launchctl unload ~/Library/LaunchAgents/life.ascendvent.context-manager.plist
launchctl load  ~/Library/LaunchAgents/life.ascendvent.context-manager.plist
```

If setting up on a new machine from scratch:
```bash
cd context-engine
python3 -m venv .venv --system-site-packages
.venv/bin/python3 -m pip install -r requirements.txt
```

## Database — schema migrations (Supabase cloud)

Supabase is the **cloud project** `rwtaxwtbwtyxcdlkozod.supabase.co`. There is no local Supabase for this project.

**Adding or changing schema:**

```bash
# 1. Create a migration file (always use this — never invent filenames)
supabase migration new <descriptive-name>

# 2. Write the SQL in supabase/migrations/<timestamp>_<name>.sql

# 3. Apply to cloud directly (db push won't work — cloud has migrations from
#    the shared checkin-ascendvent project that aren't in this repo's local dir)
supabase db query --linked -f supabase/migrations/<your-file>.sql
```

**Why not `supabase db push`?** The cloud project is shared with checkin-ascendvent. It has 24+ migrations this repo doesn't know about. `db push` rejects that mismatch. Always use `db query --linked -f` to apply migrations for this project.

**Verify after applying:**

```bash
supabase db query --linked "SELECT count(*) FROM code_embeddings;"
```

## Artifacts — output store

Every endpoint writes two places:
1. **Supabase cloud** — embedded + upserted as vector chunks (primary, searchable across sessions)
2. **Disk backup** — `~/Library/Application Support/context-store/artifacts/` (crash recovery)

**LRU eviction:** when the artifacts dir exceeds `ARTIFACTS_MAX_MB` (default 50MB), oldest files
are deleted automatically on each write. Disk copies are expendable — Supabase is the source of truth.

```bash
# Check current artifacts dir size
du -sh ~/Library/Application\ Support/context-store/artifacts/

# List files by age (oldest first)
ls -lt ~/Library/Application\ Support/context-store/artifacts/ | tail -20

# Manual wipe (safe — everything is in Supabase)
rm ~/Library/Application\ Support/context-store/artifacts/*.md

# Change the eviction threshold (edit plist then reload)
# Key: ARTIFACTS_MAX_MB — set to 0 to disable eviction
```

## Database — data re-migration

If you need to wipe and re-seed vector data (e.g. schema changed, stale embeddings):

```bash
# 1. Wipe cloud table
supabase db query --linked "TRUNCATE code_embeddings;"

# 2. Re-index via the engine (re-embeds from source files)
curl -s -X POST http://localhost:8088/index \
  -H "Content-Type: application/json" \
  -d '{"paths": ["ascendvent/checkin-ascendvent/app", "ascendvent/founderos/src"], "force": true}'
```

If you need to move data from the local Docker Supabase (checkin-ascendvent) to cloud again:

```bash
# Dump local (Docker Supabase runs on port 54322)
supabase db dump --local --data-only -f /tmp/local_export.sql

# Extract code_embeddings block (find line numbers with grep -n)
grep -n "Data for Name: code_embeddings" /tmp/local_export.sql
# Then: sed -n '<start>,<end>p' /tmp/local_export.sql > /tmp/data.sql

# Push to cloud
supabase db query --linked -f /tmp/data.sql

# Verify
supabase db query --linked "SELECT count(*) FROM code_embeddings;"

# Clean up
rm /tmp/local_export.sql /tmp/data.sql
```

## Adding a new endpoint — 4 files + 1 wire-up, always

1. `models.py` — add `FooRequest` and `FooResponse`
2. `markdown_writer.py` — add `write_foo()` returning the written path
3. `main.py` — add `@app.post("/foo")` route; after the write call add:
   ```python
   asyncio.create_task(supabase_vector.store_artifact(written))
   ```
4. `main.py /setup` — document the endpoint in the agent protocol

Use the `/endpoint` agent to scaffold the pattern.

## Context engine calls for this repo

Path prefix: `ryemyster/context-manager`
App source: `ryemyster/context-manager/context-engine/app`

Use the `context-engine` MCP server for normal agent interaction:

```text
investigate_codebase(
  task="Read-only investigation for ryemyster/context-manager. Return evidence files, conclusions, and verification."
)
```

`investigate_codebase` is the primary workflow. It delegates to the existing
Context Engine Agent, which owns planning, scans, reads, memory, verification,
and repair passes. Do not replace delegation with a caller-managed chain of
advanced MCP tools.

REST remains available for compatibility and troubleshooting:

```bash
curl -s -X POST http://localhost:8088/context \
  -H "Content-Type: application/json" \
  -d '{"task": "your task", "paths": ["ryemyster/context-manager/context-engine/app"], "focus": ["relevant", "terms"]}'
```

## Hard rules

- Never bypass `safe_resolve()` in `repo_reader.py` — it's the path traversal guard
- Never write to the repo — the engine is read-only on `REPO_ROOT`
- `config.py` is the single source of truth for all env vars — never `os.getenv()` outside it
- Always update `/setup` when adding or changing an endpoint — it's the agent contract
- Keep `mcp_server.py` transport-only; it may map, poll, and shape REST responses but must not duplicate agent logic
- Keep `mcp_http_server.py` as a persistent transport wrapper over `mcp_server.py`
- Put primary delegation tools before advanced direct tools and keep descriptions explicit about preferring `investigate_codebase`
- `CONTEXT_ENGINE_API_KEY` in `config.py` — empty = local dev bypass, non-empty = enforced; health endpoints always exempt
