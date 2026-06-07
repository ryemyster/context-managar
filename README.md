# context-engine

A local AI context and retrieval service — and a callable junior agent partner.

**You are the senior. The engine is the junior. You plan, specify, and decide. The engine scans, greps, reads, and synthesizes.**

```
Spend local tokens on recall.
Spend Claude tokens on judgment.
```

---

## What it does

Any agent — Claude Code, Codex, Zed AI, or your own scripts — can delegate work to it via REST or MCP and get structured results back. It autonomously decides what to scan, what to grep, and what to read, loops until it has real evidence, then returns a conclusion.

It also exposes individual deterministic endpoints (scan, find, summarize, context, diff, vector search) that callers can invoke directly without going through the agentic loop.

---

## Topology

```
┌─────────────────────────────────────────────────────────────────────┐
│  Callers                                                            │
│                                                                     │
│  Claude Code ──┐                                                    │
│  Codex        ─┤─ REST  ──────────────────────────────────────┐    │
│  curl/scripts ─┘    POST /agents/run                          │    │
│                      GET  /agents/run/status/{run_id}         │    │
│  Zed AI ───────── MCP stdio ──────────────────────────────┐   │    │
│  Cursor/Neovim     (mcp_server.py adapts REST → MCP)      │   │    │
└───────────────────────────────────────────────────────────┼───┼────┘
                                                            │   │
                          ┌─────────────────────────────────┘   │
                          ▼                                       │
┌─────────────────────────────────────────────────────────────────────┐
│  context-engine :8088  (native Python/uvicorn — no Docker)          │
│                                                                     │
│  Agent layer                                                        │
│  ├── agent_runner.py   — think → call tool → observe → repeat      │
│  └── tool_registry.py  — scan_directory · find_in_code · read_file  │
│                          grep · health_check                         │
│                                                                     │
│  Endpoint layer                                                     │
│  ├── /scan /find /summarize /context  — deterministic + synthesis   │
│  ├── /index /vector-search            — pgvector via Supabase       │
│  └── /diff-summary                   — reasoning model             │
│                                                                     │
│  Shared infrastructure                                              │
│  ├── ollama_client.py  — generate() · chat_with_tools() · embed()  │
│  ├── repo_reader.py    — safe_resolve() path guard (never bypass)  │
│  └── supabase_vector.py — background artifact indexing             │
└─────────────────────────────────────────────────────────────────────┘
         │                          │                      │
         ▼                          ▼                      ▼
   ~/Repos (read-only)     Ollama :11434          Supabase cloud
   REPO_ROOT               qwen2.5-coder:3b       pgvector store
                           qwen3.5:9b             rwtaxwtbwtyxcdlkozod
                           nomic-embed-text
```

### Deployment modes

| Mode | TLS | Auth | LOG_FORMAT |
|------|-----|------|------------|
| Local dev | none (localhost only) | leave `CONTEXT_ENGINE_API_KEY` unset | `text` |
| Cloud | TLS at reverse proxy | set `CONTEXT_ENGINE_API_KEY` | `json` |

For cloud: put a TLS-terminating reverse proxy (nginx, Caddy, cloud load balancer) in front of uvicorn. The engine itself always speaks plain HTTP — TLS is a proxy concern. Set `CONTEXT_ENGINE_API_KEY` to require `X-API-Key: <secret>` on every non-health request.

---

## Three-model stack

| Model | Env var | Used by |
|-------|---------|---------|
| `qwen2.5-coder:3b` | `OLLAMA_MODEL` | `/context`, `/scan`, `/find`, `/summarize`, agent tool loop |
| `qwen3.5:9b` | `OLLAMA_REASON_MODEL` | `/diff-summary`, `/agents/run` (tool-calling loop) |
| `nomic-embed-text` | `OLLAMA_EMBED_MODEL` | `/index`, `/vector-search`, `/context` (vector step) |

Routing rule: `/diff-summary` and `/agents/run` → `chat_with_tools()` / `generate_reasoning()` (judgment). Everything else → `generate()` (code pattern matching) or `embed()`.

---

## Endpoints

### Monitoring

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Full status JSON — always HTTP 200, check `status` field |
| GET | `/healthcheck` | HTTP 200 `{"ok":true}` or 503 `{"ok":false,"reason":"..."}` |
| GET | `/debug` | Model loaded, vector row count, output files, config, tips |
| GET | `/setup` | Live Markdown — any agent reads this to self-configure |

### Agent delegation (async/poll pattern)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/agents/run` | Delegate a task; returns `run_id` immediately |
| GET | `/agents/run/status/{run_id}` | Poll until `status != "running"` |
| GET | `/agents/tools` | Discover what tools the agent has |
| POST | `/tools/call` | Invoke a single tool directly |

### Context & search

| Method | Path | Model | Description |
|--------|------|-------|-------------|
| POST | `/context` | nomic → qwen | Full context bundle for a task |
| POST | `/scan` | qwen | Walk directory, extract patterns |
| POST | `/find` | qwen | Grep + synthesize matches |
| POST | `/summarize` | qwen | Summarize a single file |
| POST | `/routes` | qwen | Extract Next.js / FastAPI routes |
| POST | `/dependencies` | none | Map imports (deterministic) |
| POST | `/diff-summary` | qwen3.5:9b | Risk-annotated diff review |

### Vector store

| Method | Path | Model | Description |
|--------|------|-------|-------------|
| POST | `/index` | nomic | Embed + upsert code chunks into Supabase |
| POST | `/vector-search` | nomic | Semantic search across indexed chunks |

### Code generation (mechanical tasks only)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/draft` | Generate or edit a single file |
| POST | `/scaffold` | Generate multiple files |

Interactive API docs: http://localhost:8088/docs

---

## Agent delegation — how it works

The engine runs a think → act loop:

1. Receives a task via `POST /agents/run`
2. Calls `qwen3.5:9b` with tool definitions (scan_directory, find_in_code, read_file, grep, health_check)
3. Model calls tools → results are fed back as observations
4. Loops until the model produces a final answer, or hits max_iterations / timeout
5. Result persisted to Supabase + disk; poll `GET /agents/run/status/{run_id}` for completion

**MCP callers (Zed, Cursor, etc.)** talk to `mcp_server.py` which is a thin stdio adapter that translates MCP JSON-RPC 2.0 to the same REST endpoints. No separate process — the engine keeps running independently.

---

## Prerequisites

- **macOS** with Homebrew
- **Ollama**: `brew install ollama && ollama serve`
- **Models**: `ollama pull qwen2.5-coder:3b qwen3.5:9b nomic-embed-text`
- **Python 3.12+** on the host (system Python — the venv inherits system site-packages)
- **Supabase cloud project** (already provisioned at `rwtaxwtbwtyxcdlkozod.supabase.co`)
- **supabase CLI**: `brew install supabase/tap/supabase`

---

## Local setup

### 1. Pull Ollama models

```bash
ollama pull qwen2.5-coder:3b
ollama pull qwen3.5:9b
ollama pull nomic-embed-text
```

### 2. Configure

```bash
cp .env.example .env
```

Required env vars (set in `.env` or the launchd plist):

```
REPO_ROOT=/Users/<you>/Repos
OUTPUT_DIR=/Users/<you>/Library/Application Support/context-store/artifacts
SUPABASE_URL=https://rwtaxwtbwtyxcdlkozod.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service_role secret from Supabase Studio → Settings → API>
OLLAMA_HOST=http://localhost:11434
```

### 3. Create the venv

```bash
cd context-engine
python3 -m venv .venv --system-site-packages
# If starting fresh without system packages, install deps:
.venv/bin/python3 -m pip install -r requirements.txt
```

The venv is configured with `include-system-site-packages = true` so system-installed packages (httpx, pydantic, fastapi) are available alongside venv-only packages (pytest).

### 4. Set up the launchd service

```bash
# Install the plist (edit first to set correct REPO_ROOT / OUTPUT_DIR)
cp scripts/life.ascendvent.context-manager.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/life.ascendvent.context-manager.plist
```

Auto-starts on login, restarts on crash (`KeepAlive=true`).

### 5. Apply Supabase migrations (first time only)

```bash
supabase link --project-ref rwtaxwtbwtyxcdlkozod --yes

# Apply each migration in order
supabase db query --linked -f supabase/migrations/<file>.sql
```

**Never use `supabase db push`** — the cloud project is shared with other apps; it rejects the migration history mismatch. Always use `db query --linked -f`.

### 6. Verify

```bash
curl -s http://localhost:8088/health | python3 -m json.tool
# "status": "ok"
```

---

## Service management

```bash
# Status (col 1 = PID, col 2 = last exit code)
launchctl list | grep context-manager

# Stop
launchctl unload ~/Library/LaunchAgents/life.ascendvent.context-manager.plist

# Start
launchctl load ~/Library/LaunchAgents/life.ascendvent.context-manager.plist

# Logs (live)
tail -f ~/Library/Logs/context-manager.log
```

---

## Development workflow

### After editing any Python file

No rebuild — just restart:

```bash
launchctl unload ~/Library/LaunchAgents/life.ascendvent.context-manager.plist
launchctl load  ~/Library/LaunchAgents/life.ascendvent.context-manager.plist
curl -s http://localhost:8088/health | python3 -m json.tool
```

Use the `/rebuild` slash command in Claude Code to do this in one step.

**Never assume a change works by reading the code. Always curl the endpoint.**

### Running tests

```bash
# From the repo root
context-engine/.venv/bin/python3 -m pytest

# Verbose
context-engine/.venv/bin/python3 -m pytest -v

# Single file
context-engine/.venv/bin/python3 -m pytest tests/test_agent_runner.py -v
```

Test coverage:

| File | What's tested |
|------|--------------|
| `tests/test_agent_runner.py` | `_coerce_arguments` edge cases; all 5 stop conditions (final_answer, model_error, max_iterations, timeout, string arg coercion, custom prompts, message history) |
| `tests/test_tool_registry.py` | `get_tool_definitions`, `execute_tool` dispatch, all 5 executors (scan, find, read with paging, grep, health_check) including path rejection and truncation |
| `tests/test_mcp_server.py` | MCP JSON-RPC dispatch (initialize, ping, tools/list, tools/call, notifications, unknown methods), `_fetch_tools` schema conversion, `_call_tool` timeout |
| `tests/test_artifact_store.py` | Artifact record writes, typed vector paths |
| `tests/test_context_builder.py` | Context bundle helpers, path filtering, suggestion ranking |
| `tests/test_issue_auditor.py` | Evidence classification, rule-based recommendations, findings edge cases |

Tests use `unittest.mock` — no Ollama or filesystem calls. Safe to run offline.

### Adding a Python dependency

```bash
context-engine/.venv/bin/python3 -m pip install <package>
echo "<package>==<version>" >> context-engine/requirements.txt
# Restart service
```

### Adding a new endpoint

Four files, always: `models.py` → `markdown_writer.py` → `main.py` (route) → `main.py` (/setup doc).

Use the `/endpoint` agent: it scaffolds all four files and the store_artifact wire-up.

**`/setup` is the agent contract.** Every agent that calls this service reads `/setup` cold to understand how to use it. Update it in the same commit as the endpoint.

---

## Security

### API key (endpoint authentication)

Set `CONTEXT_ENGINE_API_KEY` to require `X-API-Key: <secret>` on all requests:

```bash
# In .env or the launchd plist
CONTEXT_ENGINE_API_KEY=your-strong-secret-here
```

- **Local dev**: leave unset — all requests pass through
- **Cloud**: always set — any request without the correct key gets HTTP 401
- **Health endpoints** (`/health`, `/healthcheck`, `/setup`) are always exempt — monitoring works without a key

```bash
# Authenticated request
curl -H "X-API-Key: your-secret" http://your-host:8088/agents/run \
  -H "Content-Type: application/json" \
  -d '{"task": "..."}'
```

### Request tracing

Every request gets a unique request ID. Pass `X-Request-Id` in headers to correlate with your own trace IDs:

```bash
curl -H "X-Request-Id: my-trace-abc123" http://localhost:8088/agents/run ...
```

The response echoes `X-Request-Id` back. All log lines for that request include `rid=<id>` (text format) or `"request_id": "<id>"` (JSON format).

### Path traversal guard

All file paths go through `safe_resolve()` in `repo_reader.py` before any read. This is a hard rule — never bypass it.

---

## Logging

### Text format (local dev default)

```
2026-06-07 14:23:01 [INFO    ] POST /agents/run 200 1247ms rid=c80a2cd8de81
2026-06-07 14:23:04 [WARNING ] POST /context 200 5823ms SLOW rid=f3a1b2c9d4e5
2026-06-07 14:23:05 [ERROR   ] store_artifact failed: connection timeout
```

### JSON format (cloud / log aggregator)

Set `LOG_FORMAT=json` for structured output compatible with Datadog, CloudWatch, Splunk, etc.:

```json
{"ts": "2026-06-07T14:23:01Z", "level": "INFO", "logger": "context-engine", "msg": "POST /agents/run 200 1247ms rid=c80a2cd8de81", "request_id": "c80a2cd8de81"}
```

### Log file

All stdout/stderr → `~/Library/Logs/context-manager.log`

```bash
# Live tail
tail -f ~/Library/Logs/context-manager.log

# Errors only
grep -i "error\|warn\|critical" ~/Library/Logs/context-manager.log | tail -30

# Trace a specific request
grep "c80a2cd8de81" ~/Library/Logs/context-manager.log

# Watch a specific endpoint
tail -f ~/Library/Logs/context-manager.log | grep "/agents/run\|/context"
```

Key log lines:
- `context-engine ready on :8088` — clean startup
- `store_artifact done` — artifact indexed to Supabase
- `store_artifact failed` — background indexing error (non-fatal; disk backup still written)
- `SLOW` — request exceeded `SLOW_REQUEST_MS` threshold (default 5s)
- `auth rejected` — bad or missing API key (cloud mode only)

### Log rotation

Configured in `scripts/context-manager.newsyslog.conf` — rotates at 10 MB, keeps 5 compressed archives.

```bash
# Install (one-time, requires sudo)
sudo cp scripts/context-manager.newsyslog.conf /etc/newsyslog.d/context-manager.conf

# Verify
sudo newsyslog -nv 2>&1 | grep context-manager
```

---

## Cloud deployment

A cloud deployment needs four things beyond local:

### 1. TLS termination

Run a reverse proxy in front of uvicorn. Example nginx config:

```nginx
server {
    listen 443 ssl;
    server_name context-engine.your-domain.com;

    ssl_certificate     /etc/letsencrypt/live/.../fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/.../privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8088;
        proxy_set_header X-Forwarded-For $remote_addr;
    }
}
```

Caddy is simpler — it auto-provisions TLS certificates:

```
context-engine.your-domain.com {
    reverse_proxy localhost:8088
}
```

uvicorn does not need TLS configuration — the proxy handles it.

### 2. API key

```bash
# In the server's environment or systemd unit
CONTEXT_ENGINE_API_KEY=<strong-random-secret>
```

Generate one: `openssl rand -hex 32`

### 3. Structured logging

```bash
LOG_FORMAT=json
LOG_LEVEL=INFO
```

Pipe stdout to your log aggregator. With systemd:

```ini
[Service]
StandardOutput=journal
StandardError=journal
```

### 4. Supabase connection

Same cloud Supabase project works from any deployment location. Set `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` in the environment.

### Cloud env checklist

```
OLLAMA_HOST=http://<ollama-host>:11434
REPO_ROOT=/path/to/repos
OUTPUT_DIR=/path/to/artifacts
SUPABASE_URL=https://rwtaxwtbwtyxcdlkozod.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<secret>
CONTEXT_ENGINE_API_KEY=<secret>
LOG_FORMAT=json
LOG_LEVEL=INFO
```

---

## MCP integration (Zed, Cursor, Neovim)

`context-engine/mcp_server.py` is a standalone MCP stdio server. Editors spawn it as a subprocess; it translates MCP JSON-RPC 2.0 calls into REST calls to `localhost:8088`.

### Zed

Add to `~/.config/zed/settings.json`:

```json
"context_servers": {
  "context-engine": {
    "enabled": true,
    "command": "/Users/<you>/Repos/ryemyster/context-manager/context-engine/.venv/bin/python",
    "args": ["/Users/<you>/Repos/ryemyster/context-manager/context-engine/mcp_server.py"]
  }
}
```

Requires the REST service to be running at `localhost:8088`. The MCP server is just an adapter — start/stop it independently.

### Other MCP-compatible editors

Same pattern: run `mcp_server.py` as the command, no args. It reads stdin and writes stdout per MCP spec.

The MCP server exposes the same 5 tools as `/agents/tools` (scan_directory, find_in_code, read_file, grep, health_check). Any editor that can call MCP tools can use all of them.

---

## Output artifacts

Every endpoint writes to two places:
1. **Supabase cloud** — embedded + upserted as vector chunks (primary, searchable across sessions)
2. **Disk backup** — `$OUTPUT_DIR` (default: `~/Library/Application Support/context-store/artifacts/`)

LRU eviction: when the disk dir exceeds `ARTIFACTS_MAX_MB` (default 50 MB), the oldest files are deleted automatically on each write. Disk copies are expendable — Supabase is the source of truth.

```bash
du -sh ~/Library/Application\ Support/context-store/artifacts/
rm ~/Library/Application\ Support/context-store/artifacts/*.md   # safe
```

---

## Database (Supabase)

### Adding or changing schema

```bash
# 1. Create a migration file
supabase migration new <descriptive-name>

# 2. Write SQL in supabase/migrations/<timestamp>_<name>.sql

# 3. Apply to cloud directly
supabase db query --linked -f supabase/migrations/<your-file>.sql

# 4. Verify
supabase db query --linked "SELECT count(*) FROM code_embeddings;"
```

### Re-seeding embeddings

```bash
# 1. Wipe
supabase db query --linked "TRUNCATE code_embeddings;"

# 2. Re-index
curl -s -X POST http://localhost:8088/index \
  -H "Content-Type: application/json" \
  -d '{"paths": ["owner/repo/src"], "force": true}'
```

---

## Troubleshooting

**Start here:**

```bash
curl http://localhost:8088/debug | python3 -m json.tool
```

The `tips` field maps each failure mode to its fix.

| Symptom | Fix |
|---------|-----|
| `/healthcheck` returns 503 | Check `reason` field: `model not available` → Ollama down; `repo not mounted` → bad `REPO_ROOT` |
| `ollama: false` in health | `curl http://localhost:11434/api/tags` — if down, run `ollama serve` |
| `vector_ready: false` | Run `/index` first; check `supabase_key_set: true` in `/debug` |
| `store_artifact failed` in logs | Non-fatal — Supabase connection issue; disk backup still written |
| `SLOW` in logs | Narrow your path scope; or cold model load (retry in 10s) |
| HTTP 401 on all requests | `CONTEXT_ENGINE_API_KEY` is set — send `X-API-Key: <secret>` header |
| `repo_mounted: false` | `REPO_ROOT` in plist/`.env` doesn't exist or isn't a directory |
| Port 8088 conflict | `lsof -i :8088` — find and stop the other process |
| Agent loop hits max_iterations | Increase `AGENT_MAX_ITERATIONS` or narrow the task scope |
