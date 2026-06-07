# Integrating context-engine with Claude Code

**context-engine is the junior. You are the senior.**

Call it via REST or MCP to delegate tasks. It autonomously scans, greps, and reads the repo, loops until it has evidence, and returns conclusions. You plan, review, and apply.

---

## Prerequisites

- context-engine running: `curl http://localhost:8088/healthcheck` → `{"ok":true,...}`
- If not: `launchctl load ~/Library/LaunchAgents/life.ascendvent.context-manager.plist`

---

## Self-configuration (fastest path)

In any new Claude Code session:

```
Run: `curl -s http://localhost:8088/setup` and use it to configure this project to use context-engine
```

Claude reads the live Markdown and handles everything: writes the `.claude/rules/context-engine.md` rule, tells you whether to run `/index`, configures the path prefix for your repo.

---

## Primary workflow — agent delegation

The engine runs an autonomous agentic loop. Delegate a task; poll for the result.

```bash
# 1. Delegate
curl -s -X POST http://localhost:8088/agents/run \
  -H "Content-Type: application/json" \
  -d '{
    "task": "Find all FastAPI route handlers in ryemyster/context-manager/context-engine/app and list them with their HTTP methods and paths"
  }' | python3 -m json.tool
# → {"run_id": "abc123...", "status": "running", "task": "..."}

# 2. Poll until complete
curl -s http://localhost:8088/agents/run/status/abc123... | python3 -m json.tool
# → {"status": "complete", "final_answer": "...", "tool_calls_made": [...], "iterations": 3}
```

**Verify it was agentic:** check `tool_calls_made` — it should show real scan/find/read calls. An empty list means the model answered from training data, not evidence.

### What the agent can do

```bash
curl -s http://localhost:8088/agents/tools | python3 -m json.tool
```

| Tool | When the agent calls it |
|------|------------------------|
| `scan_directory` | First — to discover what files exist |
| `find_in_code` | To locate where a function or concept lives |
| `read_file` | To inspect a specific file (supports offset/limit paging) |
| `grep` | Precise regex matching across files |
| `health_check` | To verify the engine is operational at session start |

### Request parameters

```json
{
  "task": "your task description",
  "tools": [],              // optional — empty = all tools enabled
  "max_iterations": 10,     // optional — stop after N think→act cycles
  "system_prompt": null     // optional — override the default agent instructions
}
```

---

## Direct endpoint workflow

For mechanical tasks where you already know what to call:

```bash
# Full context bundle for a task (the classic pre-session workflow)
curl -s -X POST http://localhost:8088/context \
  -H "Content-Type: application/json" \
  -d '{
    "task": "Add rate limiting to the agents endpoint",
    "paths": ["ryemyster/context-manager/context-engine/app"],
    "focus": ["rate limit", "middleware", "agents"]
  }' | python3 -m json.tool

# Scan a directory
curl -s -X POST http://localhost:8088/scan \
  -H "Content-Type: application/json" \
  -d '{"path": "ryemyster/context-manager/context-engine/app"}' | python3 -m json.tool

# Find where a concept lives
curl -s -X POST http://localhost:8088/find \
  -H "Content-Type: application/json" \
  -d '{"query": "safe_resolve path guard", "path": "ryemyster/context-manager/context-engine/app"}' \
  | python3 -m json.tool

# Summarize a file
curl -s -X POST http://localhost:8088/summarize \
  -H "Content-Type: application/json" \
  -d '{"file": "ryemyster/context-manager/context-engine/app/agent_runner.py"}' \
  | python3 -m json.tool
```

Scripts are available in `scripts/` for each endpoint.

---

## Configuring a new project manually

### 1. Add the context-engine rule

Create `.claude/rules/context-engine.md` in your project:

```markdown
## Context Engine — when and how to use it

A local context scout runs at http://localhost:8088. Claude is the SR dev; the scout is the JR dev.

**Availability check — always first:**
curl -s http://localhost:8088/healthcheck

**Path prefix for this repo:** owner/repo  (replace with your owner/repo)

**Decision table:**

| Situation | Endpoint | Body |
|-----------|----------|------|
| Starting any non-trivial task | POST /context | {"task": "...", "paths": ["owner/repo/src"]} |
| Delegate agentic task to the junior | POST /agents/run → poll /agents/run/status/{run_id} | |
| Need to know what is in a directory | POST /scan | {"path": "owner/repo/src"} |
| Need to find where a concept lives | POST /find | {"query": "...", "path": "owner/repo/src"} |
| Need to understand one specific file | POST /summarize | {"file": "owner/repo/path/to/file"} |
| After editing — before returning | POST /diff-summary | {"diff": "<git diff output>"} |
```

### 2. Index before each session (optional but recommended)

```bash
curl -s -X POST http://localhost:8088/index \
  -H "Content-Type: application/json" \
  -d '{"paths": ["owner/repo/src", "owner/repo/lib"]}'
```

Stores embeddings in Supabase. Re-run when the codebase changes significantly. Enables semantic `/vector-search` in `/context` calls.

---

## Daily workflow (with agent delegation)

```
1. Open Claude Code session

2. Optional — index if code changed since last session:
   POST /index {"paths": ["owner/repo/src"]}

3. For complex investigation tasks — delegate to the junior:
   POST /agents/run {"task": "Find all auth middleware and check if it covers /agents/*"}
   GET  /agents/run/status/{run_id}   ← poll until complete
   → Junior returns: final_answer + tool_calls_made (evidence trail)

4. For pre-task context loading — direct call:
   POST /context {"task": "Add auth to agent endpoints", "paths": [...], "focus": [...]}
   → Read the context bundle artifact

5. Claude reviews findings, makes judgment calls, implements

6. After Claude edits — review the diff:
   git diff | POST /diff-summary {"diff": "..."}

7. Claude reviews risk-annotated diff summary + signs off
```

---

## Authenticated requests (cloud deployment)

If the engine is deployed with `CONTEXT_ENGINE_API_KEY` set:

```bash
curl -H "X-API-Key: your-secret" -H "Content-Type: application/json" \
  http://context-engine.your-domain.com/agents/run \
  -d '{"task": "..."}'
```

Health endpoints (`/health`, `/healthcheck`, `/setup`) are always exempt — no key needed for monitoring.

---

## Monitoring endpoints quick reference

| Endpoint | Use case |
|----------|----------|
| `GET /healthcheck` | Pass/fail: HTTP 200 or 503. Use in scripts and health monitors. |
| `GET /health` | Full service status: Ollama, Supabase, models, repo. Always HTTP 200. |
| `GET /debug` | Troubleshooting: model loaded, vector row count, output files, config, tips. |
| `GET /setup` | Self-configure: any agent reads this cold to understand the full API. |

```bash
# Quick up check
curl -sf http://localhost:8088/healthcheck && echo "up" || echo "DOWN"

# Full status
curl -s http://localhost:8088/health | python3 -m json.tool

# Troubleshoot — check 'tips' field first
curl -s http://localhost:8088/debug | python3 -m json.tool
```

---

## Troubleshooting

**Empty or generic `final_answer` from `/agents/run`:**
- Check `tool_calls_made` — zero calls means the model answered without evidence
- Increase `max_iterations` or make the task more specific
- Try `GET /agents/tools` to verify all 5 tools are registered

**`/healthcheck` returns 503:**
- Check `reason` in response body
- `model not available` → `curl http://localhost:11434/api/tags` — start Ollama if down
- `repo not mounted` → verify `REPO_ROOT` in the plist points to an existing directory

**Slow responses / SLOW in logs:**
- Scope the path: `"paths": ["owner/repo/src/api"]` not `"paths": ["owner/repo"]`
- First call after restart has model cold-load overhead (~30s) — retry once

**Path rejected errors in tool results:**
- Paths must use `owner/repo/subdir` format — never bare `.` or `/`
- Example: `ryemyster/context-manager/context-engine/app` not `app` or `.`
