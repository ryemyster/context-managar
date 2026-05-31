# local-model / context-engine

This IS the context engine. Changes here affect every project that uses it as a scout.

## Project structure

```
context-engine/app/
  main.py           — all routes + /setup documentation (update /setup when adding endpoints)
  config.py         — env vars and constants — single source of truth, import from here
  ollama_client.py  — generate() · generate_reasoning() · embed()
  models.py         — Pydantic request/response models for all endpoints
  markdown_writer.py — output file formatters (one write_* function per endpoint)
  diff_reviewer.py  — /diff-summary logic
  context_builder.py — /context orchestration
  scanner.py        — /scan logic
  search_worker.py  — /find + grep logic
  route_extractor.py — /routes logic
  dependency_mapper.py — /dependencies logic
  supabase_vector.py — pgvector client
  repo_reader.py    — safe file reading + path guard (never bypass this)
docker-compose.yml  — container config
.env                — local model + Supabase overrides (gitignored)
scripts/            — shell wrappers for each endpoint
```

## Three-model stack

| Model | Env var | Used by |
|-------|---------|---------|
| `qwen3.5:9b` | `OLLAMA_REASON_MODEL` | `/diff-summary` |
| `qwen2.5-coder:3b` | `OLLAMA_MODEL` | `/context`, `/draft`, `/scaffold`, `/scan`, `/find`, `/summarize` |
| `nomic-embed-text` | `OLLAMA_EMBED_MODEL` | `/index`, `/vector-search`, `/context` (vector step) |

**Routing rule:** `/diff-summary` → `generate_reasoning()` (risk analysis is judgment). Everything else → `generate()` (code pattern matching) or `embed()`. `/context` uses code model for synthesis — relevance scoring is pattern matching, not reasoning.

## Service management (launchd)

The service runs as a native Python process — no Docker. Managed by:
`~/Library/LaunchAgents/life.ascendvent.context-manager.plist`

Auto-starts on login, restarts on crash (KeepAlive=true).

```bash
# Status — col 1 = PID, col 2 = last exit code, col 3 = label
launchctl list | grep context-manager

# Stop
launchctl unload ~/Library/LaunchAgents/life.ascendvent.context-manager.plist

# Start
launchctl load ~/Library/LaunchAgents/life.ascendvent.context-manager.plist

# Logs
tail -f ~/Library/Logs/context-manager.log
```

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

## Dev workflow — adding a Python dependency

```bash
# Install into the venv
context-engine/.venv/bin/pip install <package>

# Pin it in requirements.txt
echo "<package>==<version>" >> context-engine/requirements.txt

# Restart
launchctl unload ~/Library/LaunchAgents/life.ascendvent.context-manager.plist
launchctl load  ~/Library/LaunchAgents/life.ascendvent.context-manager.plist
```

If setting up on a new machine from scratch:
```bash
cd context-engine
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
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

## Adding a new endpoint — 4 files, always

1. `models.py` — add `FooRequest` and `FooResponse`
2. `markdown_writer.py` — add `write_foo()` 
3. `main.py` — add `@app.post("/foo")` route
4. `main.py /setup` — document the endpoint in the agent protocol

Use the `/endpoint` agent to scaffold the pattern.

## Context engine calls for this repo

Path prefix: `ryemyster/local-model`
App source: `ryemyster/local-model/context-engine/app`

```bash
curl -s -X POST http://localhost:8088/context \
  -H "Content-Type: application/json" \
  -d '{"task": "your task", "paths": ["ryemyster/local-model/context-engine/app"], "focus": ["relevant", "terms"]}'
```

## Hard rules

- Never bypass `safe_resolve()` in `repo_reader.py` — it's the path traversal guard
- Never write to `/repo` — the engine is read-only on the mounted repo
- `config.py` is the single source of truth for all env vars — never `os.getenv()` outside it
- Always update `/setup` when adding or changing an endpoint — it's the agent contract
