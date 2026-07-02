# context-engine

An AI-assisted context and retrieval service with a callable junior agent.

**You are the senior. The engine is the junior. You plan, specify, and decide. The engine scans, greps, reads, and synthesizes.**

```
Spend local tokens on recall.
Spend Claude tokens on judgment.
```

---

## What it does

Any agent — Claude Code, Codex, or another MCP host — can delegate work through the thin MCP adapter and get structured results back. Existing REST clients and scripts remain supported. The Context Engine Agent autonomously decides what to scan, grep, and read, loops until it has real evidence, verifies its conclusion, and repairs unsupported answers.

It also exposes individual deterministic endpoints (scan, find, summarize, context, diff, vector search) that callers can invoke directly without going through the agentic loop.

---

## Topology

```
┌─────────────────────────────────────────────────────────────────────┐
│  Callers                                                            │
│                                                                     │
│  Claude Code ──┐                                                    │
│  Codex        ─┤─ MCP HTTP :8089/mcp ───────────────────────┐     │
│  other hosts  ─┘  investigate_codebase · load_context      │     │
│                   review_diff · audit_issue                 │     │
│  curl/scripts ── REST compatibility ────────────────────┐   │     │
└───────────────────────────────────────────────────────────┼───┼────┘
                                                            │   │
                          ┌─────────────────────────────────┘   │
                          ▼                                       │
┌─────────────────────────────────────────────────────────────────────┐
│  context-engine :8088  (native Python/uvicorn in current deployment)│
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
│  ├── inference/       — role routing + provider normalization       │
│  ├── repo_reader.py   — safe_resolve() path guard (never bypass)   │
│  └── supabase_vector.py — background artifact indexing             │
└─────────────────────────────────────────────────────────────────────┘
         │                          │                      │
         ▼                          ▼                      ▼
   ~/Repos (read-only)   Ollama or OpenAI API      Supabase cloud
   REPO_ROOT             generation/agent roles    pgvector store
                         independent embeddings
```

### Deployment modes

| Mode | TLS | Auth | LOG_FORMAT |
|------|-----|------|------------|
| Local native | none by default | recommended because REST binds `0.0.0.0` | `text` |
| Cloud | TLS at reverse proxy | set `CONTEXT_ENGINE_API_KEY` | `json` |

The native installer starts REST on `0.0.0.0:8088`; it is not localhost-only.
Use the macOS firewall or enable `CONTEXT_ENGINE_API_KEY`. The MCP transport
defaults to `127.0.0.1:8089`. For cloud deployments, put a TLS-terminating
proxy in front of uvicorn and require the API key on every non-health request.

---

## Inference routing

| Role | New env var | Legacy default | Used by |
|------|-------------|----------------|---------|
| Fast | `INFERENCE_FAST_MODEL` | `OLLAMA_MODEL` | `/context`, `/scan`, `/find`, `/summarize`, `/draft`, `/scaffold` |
| Reasoning | `INFERENCE_REASONING_MODEL` | `OLLAMA_REASON_MODEL` | `/diff-summary` |
| Agent | `INFERENCE_AGENT_MODEL` | `OLLAMA_AGENT_MODEL` | `/agents/run` structured selection and answer generation |
| Selection fallback | `INFERENCE_SELECTION_MODEL` | `OLLAMA_AGENT_SELECT_MODEL` | `/agents/run` native tool-call fallback |
| Verification | `INFERENCE_VERIFICATION_MODEL` | `OLLAMA_AGENT_VERIFY_MODEL` | `/agents/run` evidence verification |
| Embedding | `INFERENCE_EMBEDDING_MODEL` | `OLLAMA_EMBED_MODEL` | `/index`, `/vector-search`, `/context` |

Generation and embedding providers are independent. Existing installations
default to Ollama for both. The recommended cloud migration sends generation
and agent roles to an OpenAI-compatible endpoint while keeping
`nomic-embed-text` on Ollama, preserving the existing Supabase vector corpus.
See [Inference Service](docs/inference-service.md).

Agent memory preflight is best-effort and defaults to a 10-second ceiling. The
code defaults to a 650-second per-call limit and a 3000-second total run budget;
both remain configurable through legacy `OLLAMA_AGENT_*` timeout variables.
Post-run verification defaults to 60 seconds and degrades to
`verifier_timeout` without discarding the agent result.

The agent prompt is built from the tools enabled for each run. Unsupported prose
is not accepted as a final answer before a tool returns evidence, and valid tool
calls emitted as JSON text are recovered and executed inside the agent loop. A
schema-constrained next-action pass uses the configured agent model to choose
either one enabled tool or a final answer. The selection model is used for the
native tool-call fallback.

---

## Endpoints

### Monitoring

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Full status JSON — always HTTP 200, check `status` field |
| GET | `/healthcheck` | HTTP 200 `{"ok":true}` or 503 `{"ok":false,"reason":"..."}` |
| GET | `/debug` | Model loaded, vector row count, output files, config, tips |
| GET | `/setup` | Agent usage and integration playbook |
| GET/POST | `/log-level` | Read or change the process log level |

### Agent delegation (async/poll pattern)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/agents/run` | Delegate a task; returns `run_id` immediately |
| GET | `/agents/run/status/{run_id}` | Poll until `status != "running"` |
| POST | `/agents/issue-auditor/run` | Start an evidence-based issue audit |
| GET | `/agents/issue-auditor/status/{run_id}` | Poll issue-audit status |
| GET | `/agents/tools` | Discover what tools the agent has |
| POST | `/tools/call` | Invoke a single tool directly |

### Context & search

| Method | Path | Model | Description |
|--------|------|-------|-------------|
| POST | `/context` | embedding → fast | Full context bundle for a task |
| POST | `/scan` | fast | Walk directory, return file references by default |
| POST | `/find` | fast | Grep + return match references by default |
| POST | `/read` | none | Fetch explicit file content after discovery |
| POST | `/summarize` | fast | Summarize a single file |
| POST | `/routes` | fast | Extract Next.js / FastAPI route references |
| POST | `/dependencies` | none | Map import references (deterministic) |
| POST | `/diff-summary` | reasoning | Risk-annotated diff review |

### Vector store

| Method | Path | Model | Description |
|--------|------|-------|-------------|
| POST | `/index` | embedding | Embed + upsert code chunks into Supabase |
| POST | `/vector-search` | embedding | Semantic search across indexed chunks; returns references by default |

Discovery endpoints default to `detail: "summary"` and return `{id, title,
type, score, path, summary}` references plus telemetry. Use
`detail: "standard"` for slightly richer summaries, `detail: "full"` for the
legacy inline payload, or `mode: "context_safe"` for the smallest Claude-safe
response. Use the two-call fallback for thin discovery results: call with
`mode: "context_safe"` first, then make one re-call without the mode flag when the
result has too few hits, low-content summaries, or insufficient evidence to
narrow the next read. Fetch source content intentionally with `POST /read`.

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
2. Pre-flight: auto-injects prior memory from `search_memory` (if not scope-restricted)
3. Calls the configured agent model with tool definitions: `search_memory`, `update_plan`, `scan_directory`, `find_in_code`, `read_file`, `grep`, `health_check`
4. Model calls tools → `ToolResult` envelopes (ok, error_type, retryable, recovery_hint, candidates) fed back as observations; a `needs_confirmation` error includes candidate paths so the model can self-correct on bad paths
5. Model may call `update_plan` to record its goal and steps as first-class plan state
6. Loops until the model produces a final answer, or hits max_iterations / timeout
7. Post-hoc verifier (`_verify_answer`) checks coherence of the final answer against tool evidence; if it fails, a repair pass re-runs the loop with unsupported claims injected, then re-verifies
8. Result persisted to Supabase + disk; poll `GET /agents/run/status/{run_id}` for completion

Response includes: `final_answer`, `tool_calls_made`, `iterations`, `stopped_reason` (`final_answer` | `max_iterations` | `timeout` | `model_error` | `verification_failed`), `memory_context_used`, `memory_hits`, `plan_state`, `verification` (`{passed, rationale, unsupported_claims, evidence_gap, repaired?}`)

**Scope restriction:** pass `allowed_scopes: ["repo:read"]` to limit the agent to file-exploration tools only; `memory:read` and `engine:read` tools will return `scope_denied`. `update_plan` always runs regardless of scopes.

**MCP callers** connect to the persistent Streamable HTTP service at
`http://127.0.0.1:8089/mcp`. `mcp_http_server.py` delegates to the shared thin
adapter, which translates high-level MCP calls to the same REST endpoints. It
contains no planning, verification, repository scanning, or agent loop logic.
The REST engine and MCP transport run as independent launchd services.

The primary MCP tool is `investigate_codebase`. It starts `/agents/run`, polls
the run status, and returns the completed `final_answer`, `tool_calls_made`,
`verification`, `iterations`, and `plan_state` in one MCP response. This keeps
the Context Engine Agent, not the calling orchestrator, in the junior engineer
role.

---

## Prerequisites

- **macOS** with Homebrew
- **Ollama for the default/local embedding configuration**:
  `brew install ollama && ollama serve`
- **Default Ollama models**: `qwen2.5-coder:3b`, `qwen3:4b`,
  `qwen3.5:9b`, and `nomic-embed-text`
- **Python 3.12+** on the host (system Python — the venv inherits system site-packages)
- **Supabase cloud project** (already provisioned at `rwtaxwtbwtyxcdlkozod.supabase.co`)
- **supabase CLI**: `brew install supabase/tap/supabase`

---

## Local setup

### 1. Pull Ollama models

```bash
ollama pull qwen2.5-coder:3b
ollama pull qwen3:4b
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

To use cloud generation without changing existing embeddings:

```env
INFERENCE_GENERATION_PROVIDER=openai_compatible
INFERENCE_GENERATION_ENDPOINT=https://provider.example/v1
INFERENCE_GENERATION_API_KEY=<secret>
INFERENCE_FAST_MODEL=<cloud-model>
INFERENCE_REASONING_MODEL=<cloud-model>
INFERENCE_AGENT_MODEL=<cloud-model>
INFERENCE_SELECTION_MODEL=<cloud-model>
INFERENCE_VERIFICATION_MODEL=<cloud-model>
INFERENCE_EMBEDDING_PROVIDER=ollama
INFERENCE_EMBEDDING_ENDPOINT=http://localhost:11434
INFERENCE_EMBEDDING_MODEL=nomic-embed-text
```

All five generation role names must be served by the single configured
generation endpoint. Different endpoint URLs per role are not currently
supported. See [Inference Service](docs/inference-service.md) and
[Runpod model guidance](docs/runpod-mcp-ollama.md#model-selection-by-context-engine-role).

### 3. Create the venv

```bash
cd context-engine
python3 -m venv .venv --system-site-packages
# If starting fresh without system packages, install deps:
.venv/bin/python3 -m pip install -r requirements.txt
```

The venv is configured with `include-system-site-packages = true` so system-installed packages (httpx, pydantic, fastapi) are available alongside venv-only packages (pytest).

### 4. Install or restart the native service

```bash
bash scripts/install-native.sh
```

The installer creates the venv, loads `.env`, generates the launchd plist, and
restarts the service. Environment values are baked into the plist; rerun the
installer after editing `.env`.

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
| `tests/test_agent_runner.py` | Argument coercion, stop conditions, prompts, memory, planning, verification, and repair behavior |
| `tests/test_tool_registry.py` | Registry dispatch, seven internal agent tools, scopes, recovery envelopes, paging, and truncation |
| `tests/test_mcp_server.py` | Tool ordering and schemas, REST endpoint mapping, async polling, auth forwarding, structured results, and MCP JSON-RPC dispatch |
| `tests/test_mcp_http_server.py` | Persistent Streamable HTTP transport and exposed MCP tools |
| `tests/test_inference.py` | Provider translation, role routing, timeout compatibility, and configuration fallback |
| `tests/test_inference_boundary.py` | Provider dependency boundary enforcement |
| `tests/test_artifact_store.py` | Artifact record writes, typed vector paths |
| `tests/test_context_builder.py` | Context bundle helpers, path filtering, suggestion ranking |
| `tests/test_issue_auditor.py` | Evidence classification, rule-based recommendations, findings edge cases |

Tests mock external inference and network services and use isolated temporary
filesystem state. They are safe to run offline.

### Adding a Python dependency

```bash
context-engine/.venv/bin/python3 -m pip install <package>
echo "<package>==<version>" >> context-engine/requirements.txt
# Restart service
```

### Adding a new endpoint

Update the request/response models only when the route needs typed input or an
explicit FastAPI `response_model`. Add a Markdown writer only for endpoints that
produce durable artifacts. Keep `/setup` updated when endpoint behavior changes
how agents should discover, narrow, read, or act.

**`/setup` is the agent playbook.** Every agent that calls this service reads
`/setup` cold to understand how to integrate Context Engine into a repository
and how to avoid unnecessary context consumption.

---

## Security

### API key (endpoint authentication)

Set `CONTEXT_ENGINE_API_KEY` to require `X-API-Key: <secret>` on all requests:

```bash
# In .env or the launchd plist
CONTEXT_ENGINE_API_KEY=your-strong-secret-here
```

- **Local native**: currently disabled by default, but REST binds all host interfaces
- **Cloud**: always set — any request without the correct key gets HTTP 401
- **Health endpoints** (`/health`, `/healthcheck`, `/setup`) are always exempt — monitoring works without a key
- **Persistent MCP limitation**: `mcp_server.py` can forward this key, but
  `scripts/install-mcp.sh` does not currently write it into the MCP launchd
  plist

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
INFERENCE_GENERATION_PROVIDER=openai_compatible
INFERENCE_GENERATION_ENDPOINT=https://<provider>/v1
INFERENCE_GENERATION_API_KEY=<secret>
INFERENCE_FAST_MODEL=<cloud-model>
INFERENCE_REASONING_MODEL=<cloud-model>
INFERENCE_AGENT_MODEL=<cloud-model>
INFERENCE_SELECTION_MODEL=<cloud-model>
INFERENCE_VERIFICATION_MODEL=<cloud-model>
INFERENCE_EMBEDDING_PROVIDER=ollama
INFERENCE_EMBEDDING_ENDPOINT=http://<ollama-host>:11434
INFERENCE_EMBEDDING_MODEL=nomic-embed-text
REPO_ROOT=/path/to/repos
OUTPUT_DIR=/path/to/artifacts
SUPABASE_URL=https://rwtaxwtbwtyxcdlkozod.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<secret>
CONTEXT_ENGINE_API_KEY=<secret>
LOG_FORMAT=json
LOG_LEVEL=INFO
```

---

## MCP integration

`context-engine/mcp_http_server.py` is a persistent Streamable HTTP MCP server
managed by launchd. It translates MCP calls into REST calls to the existing
service at `localhost:8088`.

### Install or restart the MCP service

```bash
bash scripts/install-mcp.sh
```

The service is registered as `life.ascendvent.context-engine-mcp`, listens only
on `127.0.0.1:8089`, and exposes MCP at `/mcp`.

### Claude Code

```bash
claude mcp add --scope user --transport http \
  context-engine http://127.0.0.1:8089/mcp
```

### Codex

```bash
codex mcp add context-engine --url http://127.0.0.1:8089/mcp
```

Configuration examples:

- `config/claude-code.mcp.json.example`
- `config/codex.config.toml.example`
- `docs/mcp-integration.md`

The MCP service requires the REST service to be running. Its launchd
configuration forwards to `http://127.0.0.1:8088` by default.

Primary tools are `investigate_codebase`, `load_context`, `review_diff`, and
`audit_issue`. Advanced direct tools expose `/scan`, `/find`, `/summarize`,
`/dependencies`, `/routes`, `/draft`, `/scaffold`, and `/vector-search`.
These discovery endpoints are
reference-first by default; use `/read` or `detail="full"` only when inline
content is intentional. Orchestrators should prefer
`investigate_codebase` instead of manually chaining advanced tools.

Runpod infrastructure management uses a separate MCP server and does not replace
Context Engine MCP. See
[Managing Runpod Ollama Infrastructure through MCP](docs/runpod-mcp-ollama.md).

---

## Output artifacts

Inference and agent workflows that produce artifacts write to two places:
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
| `/healthcheck` returns 503 | Check `reason` field: `model not available` → generation provider/model configuration; `repo not mounted` → bad `REPO_ROOT` |
| `ollama: false` in health | `curl http://localhost:11434/api/tags` — if down, run `ollama serve` |
| `vector_ready: false` | Run `/index` first; check `supabase_key_set: true` in `/debug` |
| `store_artifact failed` in logs | Non-fatal — Supabase connection issue; disk backup still written |
| `SLOW` in logs | Narrow your path scope; or cold model load (retry in 10s) |
| HTTP 401 on all requests | `CONTEXT_ENGINE_API_KEY` is set — send `X-API-Key: <secret>` header |
| `repo_mounted: false` | `REPO_ROOT` in plist/`.env` doesn't exist or isn't a directory |
| Port 8088 conflict | `lsof -i :8088` — find and stop the other process |
| Agent loop hits max_iterations | Increase `AGENT_MAX_ITERATIONS` or narrow the task scope |
| `stopped_reason: "verification_failed"` | Verifier flagged the answer twice; check `verification.unsupported_claims` and narrow the task or increase `AGENT_MAX_REPAIR_ITERATIONS` |
