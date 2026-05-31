# local-model — Context Engine

Local AI context and retrieval layer for Claude Code.

**Claude Code is the engineer. This system is the scout.**

```
Spend local tokens on recall.
Spend Claude tokens on judgment.
```

---

## Purpose

Reduce Claude Code token usage by offloading mechanical context work to local models:

| Local system owns | Claude Code owns |
|---|---|
| repo scanning | planning |
| deterministic search | architecture |
| file summaries | implementation |
| dependency mapping | code edits |
| route extraction | migrations |
| context bundles | PR review |
| diff summaries | security review |
| vector retrieval | final decisions |

---

## Architecture

```
Your repo (read-only /repo)
        │
        ▼
context-engine :8088
  ├── 1. Deterministic scan (walk, grep, regex) — zero model cost
  ├── 2. Vector search → Supabase pgvector (nomic-embed-text)
  └── 3. Synthesis → Ollama qwen2.5-coder:3b (one call per request)
        │                    │
        ▼                    ▼
  ./ai-context/*.md    founderos-ollama
  (Claude reads these)  (shared, not duplicated)
```

### Infrastructure

| Component | Where it runs |
|---|---|
| context-engine API | Native Python process (uvicorn), managed by launchd |
| Ollama models | Native macOS service on `localhost:11434` |
| Supabase (vector store) | Cloud — `rwtaxwtbwtyxcdlkozod.supabase.co` |

No Docker required.

### Model assignment

| Model | Used for | Memory |
|---|---|---|
| `qwen2.5-coder:3b` | Text generation (scan/find/context/diff) | ~1.84 GB loaded |
| `nomic-embed-text` | Embeddings (index/vector-search) | ~261 MB loaded |

⚠ With `OLLAMA_MAX_LOADED_MODELS=1`, these two models share one slot. Calling `/vector-search` then `/context` causes a ~5-10s model swap. This is expected. The embed timeout is 90s to handle this gracefully.

---

## Prerequisites

- Ollama running natively (`brew install ollama && ollama serve`)
- Models pulled: `ollama pull qwen2.5-coder:3b qwen3.5:9b nomic-embed-text`
- Python 3.12+ on the host
- Supabase cloud project (already configured — `rwtaxwtbwtyxcdlkozod.supabase.co`)

---

## Setup

### 1. Pull models

```bash
bash scripts/pull-model.sh              # pulls qwen2.5-coder:3b
ollama pull nomic-embed-text            # or via scripts
```

Or from the host (Ollama already exposed on :11434):
```bash
curl -X POST http://localhost:11434/api/pull -d '{"name":"nomic-embed-text"}'
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env`:
```
REPO_PATH=/path/to/your/actual/project
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
```

Get your service role key from:
**Supabase Studio → Settings → API → `service_role` (secret)**

### 3. Set up the venv

```bash
cd context-engine
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 4. Set up the launchd service

```bash
cp ~/Library/LaunchAgents/life.ascendvent.context-manager.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/life.ascendvent.context-manager.plist
```

The plist is at `~/Library/LaunchAgents/life.ascendvent.context-manager.plist` and configures all env vars — no separate shell sourcing needed.

### 5. Apply Supabase migrations (first time / new schema)

**Do NOT use `supabase db push`** — the cloud project is shared with other apps; `db push` rejects the mismatch. Apply SQL directly:

```bash
supabase link --project-ref rwtaxwtbwtyxcdlkozod --yes
supabase db query --linked -f supabase/migrations/<file>.sql
```

### 6. Verify

```bash
curl -s http://localhost:8088/health | python3 -m json.tool
# "status": "ok" confirms Ollama, Supabase, and repo all healthy
```

---

## Endpoints

### Core

| Method | Path | Model | Description |
|---|---|---|---|
| GET | `/health` | none | Full status JSON — always HTTP 200, check `status` field |
| GET | `/healthcheck` | none | Pass/fail — HTTP 200 `{"ok":true}` or 503 `{"ok":false,"reason":"..."}` |
| GET | `/debug` | none | Diagnostics: model loaded, vector row count, output files, config, tips |
| GET | `/setup` | none | Live Markdown — Claude reads this to self-configure any project |

### Context & Search

| Method | Path | Model | Description |
|---|---|---|---|
| POST | `/scan` | qwen | Walk directory, extract patterns |
| POST | `/find` | qwen | Grep + synthesize matches |
| POST | `/routes` | qwen | Extract Next.js routes |
| POST | `/dependencies` | none | Map imports (fully deterministic) |
| POST | `/summarize` | qwen | Summarize a single file |
| POST | `/context` | nomic → qwen | Full context bundle for a task |
| POST | `/diff-summary` | qwen | Summarize a git diff |
| POST | `/vector-search` | nomic | Semantic search via Supabase |
| POST | `/index` | nomic | Index code chunks into Supabase |

Interactive docs: http://localhost:8088/docs

---

## Usage

### Health check

```bash
# Quick pass/fail (for scripts and monitors)
curl http://localhost:8088/healthcheck

# Full status
curl http://localhost:8088/health | python3 -m json.tool
```

### Debug / troubleshoot

```bash
curl http://localhost:8088/debug | python3 -m json.tool
```

Key fields:
- `ollama_loaded` — which model is in memory right now
- `vector_row_count` — 0 means `/index` hasn't been run yet
- `config.supabase_key_set` — false means `.env` has the placeholder key
- `tips` — maps each failure mode to its fix

### Configure a new project (self-configuring)

In any Claude Code session in a new project:
```
Run: `curl -s http://localhost:8088/setup` and use it to configure this project to use context-engine
```

Claude reads the live Markdown, writes the CLAUDE.md rule, creates the slash command,
and tells you whether to run `/index`.

### Scan a directory

```bash
bash scripts/scan.sh src/app/api
```

### Find a concept

```bash
bash scripts/find.sh "stripe subscription enforcement"
bash scripts/find.sh "auth middleware" src/app
```

### Extract routes

```bash
bash scripts/routes.sh
```

### Map dependencies

```bash
bash scripts/dependencies.sh src/lib
```

### Summarize a file

```bash
bash scripts/summarize.sh src/app/api/checkins/route.ts
```

### Build a context bundle (the key workflow)

```bash
bash scripts/context.sh "Add plan enforcement to check-in generation"

# With explicit paths and focus terms:
bash scripts/context.sh \
  "Add plan enforcement to check-in generation" \
  "src/app,src/lib,supabase" \
  "auth,stripe,checkins,rate-limits"
```

Then tell Claude:
```
Read ./ai-context/context-bundle.md then implement:
"Add plan enforcement to check-in generation"
```

### Summarize a diff

```bash
git diff HEAD~1 | bash scripts/diff-summary.sh
```

### Vector search (requires indexed data)

```bash
bash scripts/vector-search.sh "stripe subscription check"
```

### Index your repo (run before a coding session)

```bash
# Index default paths
bash scripts/index.sh

# Index specific paths
bash scripts/index.sh "src/app,src/lib,supabase/migrations"

# Force re-index
bash scripts/index.sh "src/app" force
```

---

## Output Files

All context written to `./ai-context/` (gitignored):

| File | Endpoint | Description |
|---|---|---|
| `context-bundle.md` | `/context` | Full task context — primary Claude input |
| `routes.md` | `/routes` | Route map with methods |
| `scan-{slug}.md` | `/scan` | Directory scan results |
| `find-{slug}.md` | `/find` | Search results + synthesis |
| `dependencies-{slug}.md` | `/dependencies` | Import graph |
| `summary-{slug}.md` | `/summarize` | Single file summary |
| `diff-{hash}.md` | `/diff-summary` | Diff review |
| `vector-{slug}.md` | `/vector-search` | Semantic search results |
| `index-report.md` | `/index` | Indexing results |

---

## The Workflow with Claude

```
1. Start a new Claude Code session

2. Before giving Claude a task:
   bash scripts/context.sh "Your task description"
   # Wait 60-120s for context-bundle.md

3. Tell Claude:
   "Read ./ai-context/context-bundle.md
    then implement: [your task]"

4. Claude reads pre-digested context (~500 tokens)
   instead of walking the repo (~5,000+ tokens)

5. Claude verifies actual source files

6. Claude implements

7. After Claude edits:
   git diff | bash scripts/diff-summary.sh

8. Claude reviews diff summary + signs off
```

---

## Configuring a New Project

See `docs/claude-code-integration.md` for the full guide.

**Fastest path** — in any new Claude Code session:
```
Run: `curl -s http://localhost:8088/setup` and use it to configure this project to use context-engine
```

**Manual path** — copy the CLAUDE.md block from `docs/claude-rule-template.md`.

---

## Memory Expectations (M3 Air, 8 GB)

| State | Docker VM used |
|---|---|
| All services idle, no model loaded | ~3.3 GB |
| During qwen generation (scan/find/context) | ~5.1 GB (88%) |
| During nomic embedding (index/vector-search) | ~3.6 GB (62%) |
| Model swap in progress | ~5.4 GB peak (93%) |

**You will feel inference calls** — 30-90s per request, machine will be busy.
This is expected. The value is that Claude's context budget is preserved.

---

## M3 Air Warnings

- **7b model**: Do NOT pull. Would OOM during inference.
- **Concurrent requests**: Single worker enforced. Do not send parallel requests.
- **During indexing**: Don't also run scans. One model at a time.
- **After model load**: Allow 5 min for KEEP_ALIVE unload before heavy host tasks.
- **Docker memory limit**: Keep at ≤6 GB. macOS needs ≥2 GB for itself.

---

## Troubleshooting

**Start here:**
```bash
curl http://localhost:8088/debug | python3 -m json.tool
```
The `tips` field maps each failure to its fix.

**`/healthcheck` returns 503**
- Check `reason` in response: `model not available` → Ollama is down; `repo not mounted` → bad REPO_ROOT
- Check service is running: `launchctl list | grep context-manager`
- Check logs: `tail -50 ~/Library/Logs/context-manager.log`

**`ollama: false` in health**
- Check Ollama is running: `curl http://localhost:11434/api/tags`
- Start it: `ollama serve` (or it runs as a native service after install)

**`vector_ready: false` in health**
- Check the migration was applied: `supabase db query --linked "SELECT count(*) FROM code_embeddings;"`
- Check `supabase_key_set: true` in `/debug`

**`vector_row_count: 0` in /debug**
- Run `/index` first: `bash scripts/index.sh "src/app,src/lib"`

**`model timeout` in responses**
- Context budget too large. Use a more specific path: `/scan src/app/api` not `/scan .`
- Or the model hasn't loaded yet — retry after 10s

**`repo_mounted: false`**
- Check `REPO_ROOT` in the plist or `.env` points to an existing directory
- Default: `/Users/rmcdonald/Repos`

---

## Starting / Stopping

```bash
# Stop
launchctl unload ~/Library/LaunchAgents/life.ascendvent.context-manager.plist

# Start
launchctl load ~/Library/LaunchAgents/life.ascendvent.context-manager.plist

# Logs
tail -f ~/Library/Logs/context-manager.log

# Status + PID
launchctl list | grep context-manager
```

Ollama and Supabase are independent — stopping this service does not affect them.

---

## Future: MCP Gateway (v2)

The planned v2 wraps context-engine in an MCP server:

```
repo.scan(path)
repo.find(query)
repo.routes()
repo.dependencies(path)
repo.context(task, paths, focus)
repo.diff_summary(diff)
repo.vector_search(query)
```

MCP calls context-engine APIs. No duplicated logic. Not implemented in v1.
