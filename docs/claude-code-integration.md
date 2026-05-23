# Integrating context-engine with Claude Code

**context-engine is the scout. Claude Code is the engineer.**

Use this document when setting up a new project that should use context-engine for pre-session context loading.

---

## What context-engine does for you

Before you give Claude a task, context-engine:
1. Walks your repo (deterministic, zero model cost)
2. Greps for focus terms
3. Runs semantic vector search (nomic-embed-text → Supabase)
4. Synthesizes into a compact context bundle (one qwen call)

Result: `./ai-context/context-bundle.md` — a ~500-token scout report Claude reads instead of walking your repo itself (~5,000+ tokens).

---

## Prerequisites

- `context-engine` running: `curl http://localhost:8088/healthcheck` returns `{"ok":true,...}`
- If not running: `cd ~/Repos/ryemyster/local-model && bash scripts/start-context.sh`

---

## Self-configuration (fastest path)

In any new Claude Code session, just say:

```
Run: `curl -s http://localhost:8088/setup` and use it to configure this project to use context-engine
```

Claude reads the live Markdown — with current status already filled in — and handles everything:
writes the CLAUDE.md block, creates the slash command, tells you whether to run `/index`.

---

## Per-project setup (manual)

### 1. Point context-engine at your repo

In `~/Repos/ryemyster/local-model/.env`:
```
REPO_PATH=/path/to/your/project
```

Then restart (no rebuild needed):
```bash
cd ~/Repos/ryemyster/local-model
docker compose -f docker-compose.context.yml up -d
```

Verify:
```bash
curl http://localhost:8088/healthcheck
# {"ok":true,"model":"qwen2.5-coder:3b","repo":"/path/to/your/project"}
```

### 2. Add CLAUDE.md to your project

Copy the rule below into your project's `CLAUDE.md` or `.claude/rules/context-engine.md`:

```markdown
## Context Engine

A local context-engine runs at http://localhost:8088.
Before starting any non-trivial task, run:

  bash ~/Repos/ryemyster/local-model/scripts/context.sh "your task description" "src/app,src/lib" "key,terms"

Then read ./ai-context/context-bundle.md before planning or editing.

Scripts available:
  context.sh      — full context bundle (primary workflow)
  scan.sh         — scan a directory
  find.sh         — grep + synthesize
  routes.sh       — extract Next.js routes
  dependencies.sh — map imports
  summarize.sh    — summarize a single file
  diff-summary.sh — review a git diff (pipe: git diff | diff-summary.sh)
  vector-search.sh — semantic search (requires indexed data)
  index.sh        — index repo into vector store (run before session)

Health:  curl http://localhost:8088/healthcheck
Debug:   curl http://localhost:8088/debug
Setup:   curl http://localhost:8088/setup
Docs:    http://localhost:8088/docs
```

### 3. Add the /context slash command (optional)

Create `.claude/commands/context.md` in your project:

```markdown
Run the context-engine scout before implementing the task.

1. Run: bash ~/Repos/ryemyster/local-model/scripts/context.sh "$ARGUMENTS" "src/app,src/lib" ""
2. Read: ./ai-context/context-bundle.md
3. Report what was found (files, risks, vector hits), then ask what to implement.
```

Then type `/context add stripe enforcement to checkins` in Claude Code and it auto-runs.

### 4. Index your repo before each session

```bash
bash ~/Repos/ryemyster/local-model/scripts/index.sh "src/app,src/lib,supabase"
```

Stores nomic-embed-text embeddings in Supabase so `/vector-search` and `/context` return
semantically relevant results. Re-run when the codebase changes significantly.

---

## The daily workflow

```
1. Open a new Claude Code session

2. Index if code changed since last session:
   bash ~/Repos/ryemyster/local-model/scripts/index.sh "src/app,src/lib"

3. Before giving Claude a task:
   bash ~/Repos/ryemyster/local-model/scripts/context.sh \
     "Add plan enforcement to check-in generation" \
     "src/app,src/lib,supabase" \
     "auth,stripe,checkins"

4. Tell Claude:
   "Read ./ai-context/context-bundle.md then implement:
    Add plan enforcement to check-in generation"

5. Claude reads the pre-digested scout report (~500 tokens), verifies source files, implements.

6. After Claude edits, review the diff:
   git diff | bash ~/Repos/ryemyster/local-model/scripts/diff-summary.sh

7. Claude reviews diff summary + signs off.
```

---

## Switching between projects

You can only point context-engine at **one repo at a time**. To switch:

```bash
cd ~/Repos/ryemyster/local-model

# Edit REPO_PATH
sed -i '' 's|^REPO_PATH=.*|REPO_PATH=/path/to/other-project|' .env

# Restart (no rebuild needed)
docker compose -f docker-compose.context.yml up -d

# Verify the new repo is mounted
curl http://localhost:8088/healthcheck
```

---

## Monitoring endpoints

| Endpoint | Use case | Returns |
|---|---|---|
| `GET /healthcheck` | Docker health, monitors, scripts | HTTP 200 `{"ok":true}` or HTTP 503 `{"ok":false,"reason":"..."}` |
| `GET /health` | Full status check | JSON with all service states, always HTTP 200 |
| `GET /debug` | Troubleshooting | Model loaded, vector row count, all output files, config, tips |
| `GET /setup` | Configure a new project | Live Markdown Claude can read and act on |

### /healthcheck — for automation
Returns 200 only when Ollama is reachable, both models are available, and the repo is mounted.
Use this in scripts, CI, or health monitors.

```bash
curl -sf http://localhost:8088/healthcheck && echo "up" || echo "DOWN"
```

### /debug — when something feels wrong
Shows exactly what's loaded, how many vector rows exist, and what output files are in `ai-context/`:

```bash
curl http://localhost:8088/debug | python3 -m json.tool
```

Key fields to check:
- `ollama_loaded` — which model is currently in memory (null = no model loaded yet)
- `vector_row_count` — how many chunks are indexed; 0 means `/index` hasn't been run
- `config.supabase_key_set` — false means `.env` still has the placeholder key
- `tips` — common failure patterns with specific fixes

### /setup — self-configure any project
Returns live Markdown. Claude can fetch and act on it directly:

```
Run: `curl -s http://localhost:8088/setup` and use it to configure this project
```

---

## Memory expectations (M3 Air 8GB)

| Operation | Docker VM | Duration |
|---|---|---|
| Idle (no model loaded) | ~3.3 GB | — |
| `/scan`, `/find`, `/summarize` | ~5.1 GB | 30-90s |
| `/index`, `/vector-search` | ~3.6 GB | 2-10s per chunk |
| `/context` (both models) | ~5.4 GB peak | 60-150s |

One model at a time. Don't run concurrent requests. Don't pull 7b models.

---

## Troubleshooting

**Start here: `curl http://localhost:8088/debug | python3 -m json.tool`**

The `tips` field in `/debug` maps each failure to its fix. Below is the quick reference.

**Empty vector search results**
- Check `vector_row_count` in `/debug` — if 0, run `/index` first
- After running qwen endpoints, nomic needs up to 30s to swap in — built-in 90s timeout handles it
- Check `vector_ready: true` in `/health`

**Model timeout on /scan or /context**
- Scope your path: `context.sh "task" "src/app/api"` not the full repo root
- Retry once — model may have been cold-loading (~30s on first call)

**`/healthcheck` returns 503**
- Check `reason` field in the response body
- `model not available` → Ollama is down: `docker inspect founderos-ollama`
- `repo not mounted` → `REPO_PATH` in `.env` doesn't exist

**Wrong repo being scanned**
- Check `repo_root` in `/health` or `/debug`
- Update `REPO_PATH` in `.env` → `docker compose -f docker-compose.context.yml up -d`

**`supabase_key_set: false` in /debug**
- `.env` still has placeholder key — get real key from Supabase Studio → Settings → API → `service_role`

**Port 8088 taken**
- `docker ps | grep 8088` — stop the conflicting container first
- `docker compose -f docker-compose.context.yml down && docker compose -f docker-compose.context.yml up -d`
