"""
context-engine — local AI context and retrieval layer for Claude Code.

Claude Code is the engineer. This is the scout.
Spend local tokens on recall. Spend Claude tokens on judgment.

Hard rules enforced here:
- Never write to /repo
- Validate all paths (path traversal guard in repo_reader.py)
- Single worker — prevents concurrent model calls on 8GB RAM
- Graceful degradation on all external services (Ollama, Supabase)
"""

import asyncio
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from . import config
from . import ollama_client, supabase_vector
from .logger import log, request_id_var


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("context-engine starting")
    log.info("  repo_root=%s", config.REPO_ROOT)
    log.info("  ollama=%s  model=%s", config.OLLAMA_HOST, config.OLLAMA_MODEL)
    log.info("  reason_model=%s", config.OLLAMA_REASON_MODEL)
    log.info("  embed_model=%s", config.OLLAMA_EMBED_MODEL)
    log.info("  supabase=%s", config.SUPABASE_URL or "not configured")
    log.info("  log_level=%s", config.LOG_LEVEL)
    log.info("context-engine ready on :8088")
    yield
    log.info("context-engine shutting down")
from . import markdown_writer as mw
from . import artifact_store
from .models import (
    ScanRequest, FindRequest, DependenciesRequest, SummarizeRequest,
    ContextRequest, DiffRequest, VectorSearchRequest, IndexRequest, DraftRequest, ScaffoldRequest, IssueAuditRequest,
    AgentRunRequest, AgentRunResponse,
    ToolCallRequest,
    HealthResponse, ScanResponse, FindResponse, RoutesResponse,
    DependenciesResponse, SummarizeResponse, ContextResponse,
    DiffResponse, VectorSearchResponse, IndexResponse, DraftResponse, ScaffoldResponse, ScaffoldFileResult, IssueAuditResponse,
)
from . import agent_runner, tool_registry
from .repo_reader import safe_resolve, read_file, rel_path, walk_repo, build_snippet_block
from .search_worker import find_in_repo, extract_imports
from .route_extractor import extract_routes
from .dependency_mapper import map_dependencies
from .scanner import scan_directory
from .diff_reviewer import review_diff
from .context_builder import build_context
from .issue_auditor import collect_agent_evidence, findings_from_evidence, insufficient_evidence_findings
from .context_builder import _issue_numbers

app = FastAPI(
    title="context-engine",
    version="1.0.0",
    description="Local AI context layer for Claude Code. Scout, not engineer.",
    lifespan=lifespan,
)


_BYPASS_PATHS = {"/health", "/healthcheck", "/setup"}


class _AuthMiddleware(BaseHTTPMiddleware):
    """
    API key enforcement. Enabled only when CONTEXT_ENGINE_API_KEY is set.
    Skips auth for health/setup paths so monitoring works unauthenticated.
    Local dev: leave the env var unset — all requests pass through.
    Cloud: set CONTEXT_ENGINE_API_KEY to a strong secret.
    """
    async def dispatch(self, request: Request, call_next):
        if config.CONTEXT_ENGINE_API_KEY:
            if request.url.path not in _BYPASS_PATHS:
                key = request.headers.get("X-API-Key", "")
                if key != config.CONTEXT_ENGINE_API_KEY:
                    log.warning("auth rejected path=%s", request.url.path)
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "X-API-Key required"},
                        headers={"WWW-Authenticate": "ApiKey"},
                    )
        return await call_next(request)


class _RequestLog(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)
        t0 = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            dur = time.monotonic() - t0
            log.error("unhandled path=%s dur=%.3fs\n%s",
                      request.url.path, dur, traceback.format_exc())
            return JSONResponse(status_code=500, content={"detail": "internal server error"})
        finally:
            request_id_var.reset(token)
        dur = time.monotonic() - t0
        ms  = dur * 1000
        slow = ms > config.SLOW_REQUEST_MS
        lvl = log.warning if (response.status_code >= 500 or slow) else log.info
        extra = " SLOW" if slow else ""
        lvl("%s %s %d %.0fms%s rid=%s", request.method, request.url.path, response.status_code, ms, extra, rid)
        response.headers["X-Request-Id"] = rid
        return response


app.add_middleware(_RequestLog)
app.add_middleware(_AuthMiddleware)


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """
    Full health status. Always returns HTTP 200 — check 'status' field.
    Use /healthcheck for monitoring (returns 503 when critical services are down).
    """
    models         = await ollama_client.list_models()
    model_ok       = any(config.OLLAMA_MODEL        in m for m in models)
    reason_ok      = any(config.OLLAMA_REASON_MODEL in m for m in models)
    embed_ok       = any(config.OLLAMA_EMBED_MODEL  in m for m in models)
    supabase_ok    = await supabase_vector.is_supabase_reachable()
    vector_ok      = await supabase_vector.is_available()
    repo_mounted   = config.REPO_ROOT.exists() and config.REPO_ROOT.is_dir()

    # Degraded = can still scan/find/summarize but vector or reasoning is down
    # Critical = code model or repo not reachable — nothing will work
    critical = model_ok and repo_mounted
    status = "ok" if (critical and reason_ok and supabase_ok) else ("degraded" if critical else "critical")

    return {
        "status":                 status,
        "ollama":                 len(models) > 0,
        "ollama_host":            config.OLLAMA_HOST,
        "code_model":             config.OLLAMA_MODEL,
        "code_model_available":   model_ok,
        "reason_model":           config.OLLAMA_REASON_MODEL,
        "reason_model_available": reason_ok,
        "embed_model":            config.OLLAMA_EMBED_MODEL,
        "embed_model_available":  embed_ok,
        "available_models":       models,
        "supabase":               supabase_ok,
        "vector_ready":           vector_ok,
        "supabase_url":           config.SUPABASE_URL or "not configured",
        "vector_table":           config.SUPABASE_VECTOR_TABLE,
        "repo_mounted":           repo_mounted,
        "repo_root":              str(config.REPO_ROOT),
        "output_dir":             str(config.OUTPUT_DIR),
    }


@app.get("/healthcheck")
async def healthcheck():
    """
    Monitoring-friendly endpoint. Returns HTTP 200 or 503.
    200 = Ollama reachable + models available + repo mounted (Supabase optional)
    503 = critical services down (Ollama or repo missing)

    Used by Docker health check and external monitors.
    curl http://localhost:8088/healthcheck → {"ok": true} or {"ok": false, "reason": "..."}
    """
    from fastapi import Response as _Resp
    models       = await ollama_client.list_models()
    model_ok     = any(config.OLLAMA_MODEL in m for m in models)
    repo_mounted = config.REPO_ROOT.exists() and config.REPO_ROOT.is_dir()

    if not model_ok:
        return _Resp(
            content=f'{{"ok":false,"reason":"model {config.OLLAMA_MODEL!r} not available in Ollama"}}',
            status_code=503,
            media_type="application/json",
        )
    if not repo_mounted:
        return _Resp(
            content=f'{{"ok":false,"reason":"repo not mounted at {config.REPO_ROOT}"}}',
            status_code=503,
            media_type="application/json",
        )
    return {"ok": True, "model": config.OLLAMA_MODEL, "repo": str(config.REPO_ROOT)}


# ── Debug ──────────────────────────────────────────────────────────────────────

@app.get("/debug")
async def debug():
    """
    Full diagnostic: model state, vector row count, config, output files.
    Use this to troubleshoot. No model calls made.
    """
    import httpx as _httpx
    from datetime import datetime, timezone

    # --- Ollama: which model is currently loaded ---
    loaded_model = None
    try:
        async with _httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{config.OLLAMA_HOST}/api/ps")
            if r.status_code == 200:
                models_loaded = r.json().get("models", [])
                loaded_model = [m["name"] for m in models_loaded] or None
    except Exception:
        pass

    # --- Supabase: row count in vector table ---
    vector_row_count = None
    vector_error = None
    supa_key = config.SUPABASE_SERVICE_ROLE_KEY
    if supa_key and config.SUPABASE_URL:
        try:
            async with _httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(
                    f"{config.SUPABASE_URL}/rest/v1/{config.SUPABASE_VECTOR_TABLE}",
                    params={"select": "id", "limit": "0"},
                    headers={
                        "apikey": supa_key,
                        "Authorization": f"Bearer {supa_key}",
                        "Prefer": "count=exact",
                    },
                )
                cr = r.headers.get("content-range", "")
                if "/" in cr:
                    vector_row_count = int(cr.split("/")[1])
                elif r.status_code != 200:
                    vector_error = f"HTTP {r.status_code}: {r.text[:120]}"
        except Exception as e:
            vector_error = str(e)

    # --- Output files ---
    output_files = []
    if config.OUTPUT_DIR.exists():
        for f in sorted(config.OUTPUT_DIR.iterdir()):
            if f.is_file() and f.name != ".gitkeep":
                stat = f.stat()
                output_files.append({
                    "name": f.name,
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                })

    # --- Config (safe — no secret values) ---
    cfg = {
        "repo_root":            str(config.REPO_ROOT),
        "output_dir":           str(config.OUTPUT_DIR),
        "ollama_host":          config.OLLAMA_HOST,
        "ollama_model":         config.OLLAMA_MODEL,
        "ollama_embed_model":   config.OLLAMA_EMBED_MODEL,
        "ollama_timeout":       config.OLLAMA_TIMEOUT,
        "ollama_num_ctx":       config.OLLAMA_NUM_CTX,
        "ollama_num_predict":   config.OLLAMA_NUM_PREDICT,
        "supabase_url":         config.SUPABASE_URL or "not set",
        "supabase_key_set":     bool(supa_key and supa_key != "your-service-role-key-here"),
        "vector_table":         config.SUPABASE_VECTOR_TABLE,
        "match_function":       config.SUPABASE_MATCH_FUNCTION,
        "max_file_bytes":       config.MAX_FILE_BYTES,
        "max_files_per_scan":   config.MAX_FILES_PER_SCAN,
        "max_total_chars":      config.MAX_TOTAL_CHARS,
    }

    return {
        "timestamp":         datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "ollama_loaded":     loaded_model,
        "vector_row_count":  vector_row_count,
        "vector_error":      vector_error,
        "output_files":      output_files,
        "config":            cfg,
        "tips": {
            "no_vector_results":   "Run /index first. Check vector_row_count > 0. embed timeout is 90s — retry after qwen unloads.",
            "model_timeout":       "Scope your path. Try /scan on src/app/api not /scan on . (root).",
            "wrong_repo":          "Set HOME in .env and restart: docker compose up -d --build",
            "model_swap_slowness": "After /context, qwen is loaded. /vector-search needs ~10-30s to swap in nomic. Built-in 90s timeout handles it.",
            "ollama_unreachable":  "Check founderos-ollama is running: docker inspect founderos-ollama",
            "supabase_401":        "SUPABASE_SERVICE_ROLE_KEY is wrong or placeholder. Get it from Supabase Studio → Settings → API.",
        }
    }


# ── Setup ──────────────────────────────────────────────────────────────────────

@app.get("/setup", response_class=__import__("fastapi").responses.PlainTextResponse)
async def setup():
    """
    Agent instruction protocol. Any AI agent can fetch this to understand how to
    integrate with context-engine. Live state is embedded — no stale docs.
    Audience: AI agent tools (Claude Code, Cursor, Copilot, custom agents).
    NOT a setup/install guide — this is an operational integration contract.
    """
    models   = await ollama_client.list_models()
    model_ok = any(config.OLLAMA_MODEL       in m for m in models)
    embed_ok = any(config.OLLAMA_EMBED_MODEL in m for m in models)
    vec_ok   = await supabase_vector.is_available()
    repo     = str(config.REPO_ROOT)
    base     = "http://localhost:8088"

    reason_ok  = any(config.OLLAMA_REASON_MODEL in m for m in models)
    gen_status    = "available" if model_ok  else "OFFLINE — skip /draft /scaffold /scan /find /summarize"
    reason_status = "available" if reason_ok else "OFFLINE — /context and /diff-summary will degrade"
    emb_status    = "available" if embed_ok  else "OFFLINE — skip /index and /vector-search"
    vec_status    = "ready" if vec_ok else "not indexed — run /index first; /vector-search returns empty until then"

    return (
        "# context-engine — Agent Integration Protocol\n"
        "> You are an AI agent. This service is your local context scout and code delegation layer.\n"
        "> To load this document: `curl -s http://localhost:8088/setup`\n"
        "> Do not ask the user to explain the codebase — call the scout instead.\n"
        "\n---\n\n"

        "## What this service does\n\n"
        "Four jobs, in priority order:\n\n"
        "1. **Retrieval** — finds relevant files and code without burning your token budget on recall\n"
        "2. **Agent delegation** — runs bounded junior-agent workflows such as issue audits\n"
        "3. **Reasoning synthesis** — uses a local reasoning model to judge what matters and what's risky\n"
        "4. **Code delegation** — lets you hand off mechanical code generation to a local code model\n\n"
        "You own all planning, architecture, security decisions, and every file write.\n\n"
        "**Output flow:** `/context` and agent endpoints write a canonical JSON record first, append an `events.jsonl` ledger entry, "
        "render Markdown for human/agent recovery, then optionally embed the JSON record with Ollama and upsert that derived memory into Supabase. "
        "The vector store is a rebuildable index; local JSON artifacts are the durable source of truth.\n"
        "**Hard constraint:** this service never writes to the repo.\n\n"
        f"**Path convention:** `REPO_ROOT` is `{repo}`. All `path` parameters must be prefixed with "
        "`<owner>/<repo>/` — e.g. `\"ascendvent/checkin-ascendvent/src\"`. Never use bare `\".\"` — it scans all of `~/Repos`.\n"
        "\n---\n\n"

        "## Three-model stack\n\n"
        "| Model | Role | Called by |\n"
        "|-------|------|-----------|\n"
        f"| `{config.OLLAMA_REASON_MODEL}` | Reasoning — judgment, risks, what matters | `/diff-summary` |\n"
        f"| `{config.OLLAMA_MODEL}` | Code — pattern matching, generation, summarisation | `/context`, `/draft`, `/scaffold`, `/scan`, `/find`, `/summarize` |\n"
        f"| `{config.OLLAMA_EMBED_MODEL}` | Embeddings — turns repo chunks and artifact records into Supabase vectors | `/index`, `/vector-search`, `/context`, agent artifact indexing |\n\n"
        "**Note:** `qwen3:4b` is installed in Ollama but is NOT used by this service — "
        "it appears in `/health` `available_models` but has no routing assignment. Ignore it.\n\n"
        "**Why the split matters:**\n"
        "- `/diff-summary` uses the reasoning model — risk analysis and test recommendations are judgment.\n"
        "- `/context` uses the code model — relevance scoring is pattern matching, not reasoning. "
        "Also avoids a slow 9b cold-load after the embed step.\n"
        "- Artifact memory uses Ollama embeddings (`nomic-embed-text`) before Supabase upsert. "
        "Supabase stores vectors; Ollama creates them.\n"
        "- `/draft` and `/scaffold` use the code model because generating code from a clear spec "
        "is pattern matching, not reasoning.\n"
        "- You (Claude Code) are the SR dev. The code model is the JR dev. "
        "You plan, delegate, review, and apply — it types.\n"
        "\n---\n\n"

        "## Live status\n\n"
        "| Capability | Status |\n"
        "|---|---|\n"
        f"| Reasoning model (`{config.OLLAMA_REASON_MODEL}`) | {reason_status} |\n"
        f"| Code model (`{config.OLLAMA_MODEL}`) | {gen_status} |\n"
        f"| Embeddings (`{config.OLLAMA_EMBED_MODEL}`) | {emb_status} |\n"
        f"| Vector index | {vec_status} |\n"
        f"| Repo | `{repo}` |\n"
        "\n---\n\n"

        "## Decision rules\n\n"
        "| Situation | Call |\n"
        "|---|---|\n"
        "| Delegate any agentic task to the junior | `POST /agents/run` → poll `GET /agents/run/status/{run_id}` |\n"
        "| Discover what tools the junior has | `GET /agents/tools` |\n"
        "| Starting any non-trivial task | `POST /context` — always start here |\n"
        "| Need to know what's in a directory | `POST /scan` |\n"
        "| Need to find where a concept lives | `POST /find` |\n"
        "| Need all routes in a Next.js app | `POST /routes` |\n"
        "| Need to understand one specific file | `POST /summarize` |\n"
        "| Need the import graph of a path | `POST /dependencies` |\n"
        "| Reviewing a git diff | `POST /diff-summary` |\n"
        "| Want semantically similar code chunks | `POST /vector-search` |\n"
        "| One file to generate or edit — spec is clear | `POST /draft` — JR dev delegation |\n"
        "| Feature spans multiple files — want to preserve context window | `POST /scaffold` — multi-file delegation |\n"
        "| Auditing GitHub issues against repo evidence | `POST /agents/issue-auditor/run` → poll `GET /agents/issue-auditor/status/{run_id}` |\n"
        "| Novel architecture, security, complex logic | Calling agent only — do not delegate |\n"
        "| Engine unreachable | Proceed without it — never block on the scout |\n\n"
        f"**Skip when:** one-liner task, files already in context, or `{base}/healthcheck` returns non-200.\n\n"
        f"**Troubleshoot:** `GET {base}/debug` — model state, vector row count, config. No model calls.\n"
        "**Agent integration rule:** call deterministic endpoints or agent endpoints first, read `artifacts.record`, "
        "then optionally use vector search for recall. Do not use vector hits as closure evidence without verifying source files.\n"
        "\n---\n\n"

        "## Endpoint reference\n\n"

        "### POST /agents/run — delegate any task to the junior agent\n"
        "The agent autonomously decides what to scan, grep, and read. It loops until it has enough evidence, "
        "then returns a final answer with a complete tool-call trace. "
        "**Returns immediately** with `run_id` and `status: \"running\"`. "
        "Poll `GET /agents/run/status/{run_id}` until status is no longer `\"running\"`.\n\n"
        "```json\n"
        '{  "task": "Find all route handlers in ryemyster/context-manager/context-engine/app and list them with their HTTP methods",\n'
        '   "tools": [],\n'
        '   "max_iterations": 10  }\n'
        "```\n"
        "- `tools`: empty = all tools enabled. Restrict to `[\"scan_directory\", \"read_file\"]` to limit scope.\n"
        "- `max_iterations`: default 10. Increase for complex multi-file tasks.\n"
        "- `system_prompt`: optional — override the default system prompt.\n"
        "Poll response fields: `final_answer` (the agent's conclusion), `tool_calls_made` (what it called and why), "
        "`iterations`, `stopped_reason` (`final_answer` | `max_iterations` | `timeout` | `model_error`).\n\n"
        "**Key proof of agentic behavior:** check `tool_calls_made` — non-empty means the agent actually explored "
        "the repo autonomously, not just generated text.\n\n"

        "### GET /agents/tools — tool manifest for agent discovery\n"
        "Returns the JSON schemas for all tools the junior can call. "
        "Any calling agent reads this to know exactly what the junior is capable of — no hardcoding required. "
        "Schema format is OpenAI/Ollama/MCP-compatible.\n"
        "Available tools: `scan_directory`, `find_in_code`, `read_file`, `grep`, `health_check`, `search_memory`.\n\n"

        "### POST /context — use before every non-trivial task\n"
        "Model: code. Scans only requested paths, greps focus terms in scope, optionally runs vector search, "
        "post-filters suggestions to existing scoped files, and synthesises what matters.\n\n"
        "```json\n"
        '{  "task": "add rate limiting to the check-in API",\n'
        '   "paths": ["ascendvent/checkin-ascendvent/src/app/api", "ascendvent/checkin-ascendvent/src/lib"],\n'
        '   "focus": ["rate-limit", "middleware", "checkins"]  }\n'
        "```\n"
        "Returns: `summary`, `suggested_files`, `risks`, `vector_hits`, `warnings`, `dropped_candidates`, `artifacts`, `written_to` "
        "→ read `context-bundle.md` or the structured artifact record.\n\n"

        "### POST /agents/issue-auditor/run — async junior PM/admin issue audit\n"
        "Model: code. Delegated workflow for evidence-based issue triage. **Returns immediately** with `run_id` and `status: \"running\"`. "
        "Poll `GET /agents/issue-auditor/status/{run_id}` until status is no longer `\"running\"`.\n\n"
        "```json\n"
        '{  "repo": "ryemyster/ShaleYeah",\n'
        '   "paths": ["agents", "servers", "sdk", "orchestrator"],\n'
        '   "task": "Audit issues #363-#376 for Tier 2 standalone-agent migration completion",\n'
        '   "requirements": ["src/agent implementation required", "agent.test.ts required", "mcp-client.test.ts required"],\n'
        '   "focus": ["issue 363", "issue 376", "src/agent/index.ts", "agent.test.ts", "mcp-client.test.ts"] }\n'
        "```\n"
        "Returns immediately: `run_id`, `status: \"running\"`, `artifacts.record` (path to poll). "
        "Poll: `GET /agents/issue-auditor/status/{run_id}` → same shape; `status` becomes `complete`, `insufficient_evidence`, or `error` when done. "
        "Each finding contains `issue`, `status`, `evidence_files`, `missing_evidence`, and `recommendation`. "
        "Status and recommendation are rule-based from the evidence matrix; the model does not override closure decisions.\n\n"

        "### POST /scan\n"
        'Model: code. `{"path": "ascendvent/checkin-ascendvent/src/app/api"}` → file list, patterns · `scan-<slug>.md`\n\n'

        "### POST /find\n"
        'Model: code. `{"query": "stripe enforcement", "path": "ascendvent/checkin-ascendvent/src"}` → matches + synthesis · `find-<slug>.md`\n\n'

        "### POST /summarize\n"
        'Model: code. `{"file": "ascendvent/checkin-ascendvent/src/app/api/checkins/route.ts"}` → purpose, deps, risks · `summary-<slug>.md`\n\n'

        "### POST /routes\n"
        'Model: code. `{"path": "ascendvent/checkin-ascendvent"}` → api_routes, page_routes, middleware, auth_paths · `routes.md`\n\n'

        "### POST /dependencies\n"
        'No model. `{"path": "ascendvent/checkin-ascendvent/src/lib"}` → import graph · `dependencies-<slug>.md`\n\n'

        "### POST /diff-summary\n"
        "Model: reasoning (`think: false` — chain-of-thought disabled for speed). "
        f"`{{\"diff\": \"<git diff output>\"}}` → summary, risks, test_recommendations · `diff-<hash>.md`\n\n"
        "**Input contract:** pass raw `git diff` output — unified diff format with file headers and hunks. "
        "The model reads actual changed lines, not descriptions. "
        "Do NOT summarize the diff yourself or pass a description — pass the diff text verbatim.\n\n"
        "**Quality depends on specificity:** a 10-line focused diff yields precise risks. "
        "A 500-line diff yields generic risks. Scope your diff when detail matters: "
        "`git diff HEAD -- path/to/file.py`\n\n"
        f"**Size limit — paginate above ~{config.DIFF_MAX_CHARS:,} characters (~130 diff lines):**\n"
        "Content beyond this is silently truncated and risks become generic or incomplete. "
        "If your diff is large, split by file or logical group and call `/diff-summary` once per chunk:\n"
        "```bash\n"
        "# Per-file: scope the diff\n"
        "git diff HEAD -- src/auth/middleware.py   # one call\n"
        "git diff HEAD -- src/api/routes.py        # separate call\n\n"
        "# Or by directory\n"
        "git diff HEAD -- src/auth/\n"
        "git diff HEAD -- src/api/\n"
        "```\n"
        "Merge the `risks` and `test_recommendations` arrays from each call — "
        "each chunk produces independent findings.\n\n"
        "**What it returns:**\n"
        "- `summary` — what changed and why it matters\n"
        "- `risks` — concrete regression or breakage risks (empty list = low risk)\n"
        "- `test_recommendations` — specific things to verify, not generic advice\n"
        "- `files_touched` — deterministic list extracted from diff headers\n\n"
        "Call after every set of edits before returning to the user.\n\n"

        "### POST /vector-search\n"
        'Model: embeddings. `{"query": "auth session middleware", "limit": 8}` → ranked chunks · `vector-<slug>.md`\n'
        "Embeds the query with Ollama, searches Supabase pgvector, and returns matching repo or artifact-memory chunks. "
        "Requires `/index` to have been run for repo-code search. Check `vector_ready` in `/health` first.\n\n"

        "### POST /index\n"
        'Model: embeddings. `{"paths": ["ascendvent/checkin-ascendvent/src/app", "ascendvent/checkin-ascendvent/src/lib"], "force": false}`\n'
        "Run once per session when code has changed. Required before `/vector-search` is useful.\n\n"

        "### POST /draft — JR dev delegation, single file\n"
        "Model: code. Use when you have a clear spec for one file and want to delegate the typing.\n"
        "You review the output and apply it yourself — you own the write.\n\n"
        "```json\n"
        '{  "task": "add a createdBy field to the Checkin type and the insert call",\n'
        '   "file": "ascendvent/checkin-ascendvent/src/lib/types.ts",\n'
        '   "context_files": ["ascendvent/checkin-ascendvent/src/app/api/checkins/route.ts"],\n'
        '   "mode": "edit"  }\n'
        "```\n"
        "- `mode: \"edit\"` — reads the existing file, applies the change\n"
        "- `mode: \"create\"` — generates a new file; uses context_files as pattern reference\n"
        "- Returns `{file, mode, code, written_to}` → read `draft-<slug>.md`, verify, then apply\n\n"

        "### POST /scaffold — context window preservation, multi-file\n"
        "Model: code. Use when a feature spans multiple files and you want to preserve your context window "
        "for orchestration and review rather than mechanical generation.\n"
        "Plan the feature yourself first. Pass the file list with a spec per file. Review the batch before applying anything.\n\n"
        "```json\n"
        '{  "task": "scaffold a notifications feature",\n'
        '   "files": [\n'
        '     {"file": "ascendvent/checkin-ascendvent/src/lib/notifications.ts", "spec": "type definitions and helper functions", "mode": "create"},\n'
        '     {"file": "ascendvent/checkin-ascendvent/src/app/api/notifications/route.ts", "spec": "GET and POST handlers following existing route patterns", "mode": "create"}\n'
        '   ],\n'
        '   "context_files": ["ascendvent/checkin-ascendvent/src/lib/types.ts", "ascendvent/checkin-ascendvent/src/app/api/checkins/route.ts"]  }\n'
        "```\n"
        "- `context_files` are read once and passed to every file generation — keep patterns consistent\n"
        "- Files are generated sequentially (one model in memory at a time)\n"
        "- Returns `{task, files: [{file, mode, code, written_to}], total, errors}`\n"
        "- Each file written to `scaffold-<slug>.md` — review all before applying any\n\n"
        "**Use /draft or /scaffold for:** CRUD routes, unit tests, schema migrations, new components that mirror existing ones, "
        "implementing a clearly-specced function, adding fields to existing models.\n\n"
        "**Do NOT use /draft or /scaffold for:** novel architecture, auth/security paths, complex multi-system logic, "
        "anything where the spec itself requires judgment — plan with Claude first.\n"
        "\n---\n\n"

        "## Output files\n\n"
        "All output written to `~/Library/Application Support/context-store/artifacts/`. Read the file — don't just use the API response.\n\n"
        "| File | Endpoint | Re-use if |\n"
        "|---|---|---|\n"
        "| `events.jsonl` | structured ledger | debugging, recovery, replay |\n"
        "| `records/<event_id>.json` | `/context`, agents | durable source of truth |\n"
        "| `markdown/<event_id>.md` | `/context`, agents | rendered view of structured record |\n"
        "| `context-bundle.md` | `/context` | same task this session |\n"
        "| `routes.md` | `/routes` | routes unchanged |\n"
        "| `scan-<slug>.md` | `/scan` | same path, no code changes |\n"
        "| `find-<slug>.md` | `/find` | same query |\n"
        "| `summary-<slug>.md` | `/summarize` | file not edited since |\n"
        "| `diff-<hash>.md` | `/diff-summary` | same diff |\n"
        "| `vector-<slug>.md` | `/vector-search` | same query |\n"
        "| `draft-<slug>.md` | `/draft` | re-draft if output unsatisfactory |\n"
        "| `scaffold-<slug>.md` | `/scaffold` | re-scaffold individual files if needed |\n\n"
        "**Always verify actual source files before editing** — output files are scout reports, not ground truth.\n"
        "Vectors are created with Ollama embeddings and stored in Supabase as a rebuildable search index. "
        "Do not treat Supabase vector rows as the canonical record of a run.\n"
        "\n---\n\n"

        "## Worked examples\n\n"
        "**Agent delegation (/agents/run) — the primary pattern:**\n"
        "```\n"
        "Task: find all route handlers in this repo\n\n"
        "1. GET /agents/tools          → understand what the junior can do\n"
        "2. POST /agents/run           → {\"task\": \"...\", \"max_iterations\": 10}\n"
        "   ← {run_id, status: 'running'}\n"
        "3. GET /agents/run/status/{run_id}  → poll until status != 'running'\n"
        "4. Read final_answer + verify tool_calls_made shows real exploration\n"
        "5. Verify source files before acting on the answer\n"
        "```\n\n"
        "**Single file delegation (/draft):**\n"
        "```\n"
        "Task: add a createdBy field to the checkin model\n\n"
        "1. POST /context  →  read context-bundle.md  →  find types.ts + route.ts\n"
        "2. POST /summarize on types.ts  →  understand current model shape\n"
        "3. Spec is clear + mechanical  →  POST /draft on types.ts\n"
        "4. Read draft-*.md  →  verify types, imports, no regressions\n"
        "5. Apply with Write/Edit  →  POST /draft on route.ts if needed\n"
        "6. POST /diff-summary  →  read risks + test recommendations\n"
        "```\n\n"
        "**Issue audit delegation (/agents/issue-auditor/run):**\n"
        "```\n"
        "Task: audit issue completion from repo evidence\n\n"
        "1. POST /agents/issue-auditor/run  →  receive {run_id, status: 'running'}\n"
        "2. GET /agents/issue-auditor/status/{run_id}  →  poll until status != 'running'\n"
        "3. Read findings and artifacts.record (JSON path)\n"
        "4. Verify evidence files before closing or commenting on issues\n"
        "5. Use GitHub tooling only after the senior agent confirms recommendations\n"
        "```\n\n"

        "**Multi-file delegation (/scaffold):**\n"
        "```\n"
        "Task: scaffold a notifications feature (3 new files)\n\n"
        "1. POST /context  →  understand existing patterns (types, routes, components)\n"
        "2. Plan the feature yourself: which files, what each one does\n"
        "3. POST /scaffold with all 3 files + context_files for pattern reference\n"
        "4. Read each scaffold-*.md  →  review the batch\n"
        "5. Apply files one by one, editing inline where needed\n"
        "6. POST /diff-summary  →  read risks across all changes\n"
        "```\n"
        "\n---\n\n"

        "## Adding this to a project permanently\n\n"

        "### Claude Code — add to `CLAUDE.md`\n\n"
        "```\n"
        "## Context Engine\n\n"
        f"Local context scout and code delegation layer at {base}.\n\n"
        "Before any non-trivial task: POST /context, read context-bundle.md from artifacts dir.\n"
        "For mechanical single-file work: POST /draft, review draft-*.md, apply manually.\n"
        "For multi-file features: POST /scaffold, review each scaffold-*.md, apply manually.\n"
        "After edits: POST /diff-summary with git diff output, read risks.\n"
        "Scout is read-only — you own all file writes.\n"
        f"If {base}/healthcheck returns non-200, proceed without it.\n"
        "```\n\n"

        "### Codex CLI — add to `~/.codex/AGENTS.md` (global) or `AGENTS.md` in project root\n\n"
        "```markdown\n"
        "## Local Context Engine\n\n"
        f"A local read-only context scout and junior developer service is available at `{base}`.\n\n"
        "Use it for non-trivial repository work when it is reachable. It retrieves relevant code,\n"
        "performs semantic search with local embeddings, summarizes changes, and can draft mechanical\n"
        "edits. It never writes to a repository. You own architecture decisions, review every draft,\n"
        "and apply every file change yourself.\n\n"
        "**Availability:** check `GET /healthcheck`. If non-200 or unreachable, continue without it.\n\n"
        f"**Full integration protocol:** `GET {base}/setup`\n\n"
        f"**Path convention:** REPO_ROOT is `{repo}`. Every path/file value must include the\n"
        "`<owner>/<repo>/` prefix. Never pass bare `.` or an absolute path.\n\n"
        "**Sandbox access rules:** Codex runs in a sandboxed environment where localhost connections\n"
        "can hang or be blocked mid-stream even when the service is healthy. Always follow these rules:\n"
        "- Use `curl -4` (force IPv4) for all context engine calls\n"
        "- Keep calls sequential — no parallel curl calls\n"
        "- Use short `--max-time` values: 10s for lightweight endpoints, 20s for `/vector-search` and `/context`\n"
        "- If a call hangs or is blocked, fall back to direct file reads rather than stalling the task\n"
        "- Do not treat the engine as broken unless `/healthcheck` fails outside the sandbox\n\n"
        "**Standard workflow:**\n"
        "1. `POST /context` before any non-trivial task — read `context-bundle.md`\n"
        "2. Inspect actual source files before deciding\n"
        "3. `POST /draft` or `/scaffold` only for clearly specified mechanical work — review before applying\n"
        "4. After edits: `git diff HEAD | ...` → `POST /diff-summary` — read risks\n"
        "```\n\n"

        "### Qwen Code — add to `~/.qwen/AGENTS.md` (global) or `AGENTS.md` in project root\n\n"
        "Qwen Code follows the same AGENTS.md convention as Codex CLI. Use the identical block above,\n"
        "placed in `~/.qwen/AGENTS.md` for global scope or a project-level `AGENTS.md` for repo scope.\n"
        "Qwen Code picks up `AGENTS.md` from the working directory and from its config home.\n"
        "\n---\n"
        f"_context-engine · repo: `{repo}` · {base}_\n"
    )


# ── Scan ───────────────────────────────────────────────────────────────────────

@app.post("/scan")
async def scan(req: ScanRequest):
    """
    Scan a directory. Walk files → extract deps → one model synthesis call.
    Writes: /output/scan-{slug}.md
    """
    t0 = time.monotonic()
    log.debug("POST /scan path=%s", req.path or "/")
    if not req.path or req.path in (".", "/"):
        raise HTTPException(status_code=400, detail="path must be scoped to a subdirectory — bare '.' or empty string would scan all of REPO_ROOT")
    base = safe_resolve(req.path)
    if not base.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {req.path!r}")

    result = await scan_directory(req.path)

    written = mw.write_scan(
        path=req.path,
        files=result["files"],
        summary=result["summary"],
        patterns=result["patterns"],
        dependencies=result["dependencies"],
    )

    asyncio.create_task(supabase_vector.store_artifact(written))
    log.debug("POST /scan done files=%d dur=%.2fs", len(result["files"]), time.monotonic() - t0)
    return {
        "path":         req.path,
        "files":        result["files"],
        "summary":      result["summary"],
        "patterns":     result["patterns"],
        "dependencies": result["dependencies"],
        "written_to":   written,
    }


# ── Find ───────────────────────────────────────────────────────────────────────

@app.post("/find")
async def find(req: FindRequest):
    """
    Grep the repo for query terms. One model synthesis call on top matches.
    Writes: /output/find-{slug}.md
    """
    t0 = time.monotonic()
    log.debug("POST /find query=%r path=%s", req.query, req.path or "/")
    matches = find_in_repo(req.query, req.path, max_results=config.MAX_SNIPPETS_PER_QUERY)

    # Build snippet block from top matches
    match_snippets: list[tuple[str, str]] = []
    for m in matches[:10]:
        try:
            f = safe_resolve(m["path"])
            content = read_file(f)
            if content:
                match_snippets.append((m["path"], content))
        except Exception:
            pass

    if match_snippets:
        snippet_block = build_snippet_block(match_snippets)
        prompt = f"""Query: "{req.query}"
Files: {snippet_block}
Where is "{req.query}" implemented? Key files? (2-3 sentences)"""
        synthesis = await ollama_client.generate(prompt)
    else:
        synthesis = "No matches found for query."

    written = mw.write_find(
        query=req.query,
        path=req.path,
        matches=matches,
        synthesis=synthesis,
    )

    asyncio.create_task(supabase_vector.store_artifact(written))
    log.debug("POST /find done matches=%d dur=%.2fs", len(matches), time.monotonic() - t0)
    return {
        "query":      req.query,
        "path":       req.path,
        "matches":    [m["path"] for m in matches],
        "files":      [m["path"] for m in matches],
        "written_to": written,
    }


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.post("/routes")
async def routes():
    """
    Extract all Next.js routes. Deterministic + one model analysis call.
    Writes: /output/routes.md
    """
    t0 = time.monotonic()
    log.debug("POST /routes")
    routes_data = extract_routes()

    api_routes = routes_data["api_routes"]
    if api_routes:
        # Build snippets from API routes for model analysis
        route_snippets: list[tuple[str, str]] = []
        for r in api_routes[:15]:
            try:
                f = safe_resolve(r["path"])
                content = read_file(f)
                if content:
                    route_snippets.append((r["path"], content))
            except Exception:
                pass

        snippet_block = build_snippet_block(route_snippets)
        prompt = f"""Next.js routes. One line each: HTTP method, auth required, what it does.
{snippet_block}"""
        analysis = await ollama_client.generate(prompt)
    else:
        analysis = "No API route files detected."

    written = mw.write_routes(routes_data, analysis)

    all_routes = list(dict.fromkeys(
        [r["path"] for r in routes_data["api_routes"]] +
        routes_data["page_routes"]
    ))

    asyncio.create_task(supabase_vector.store_artifact(written))
    log.debug("POST /routes done api=%d pages=%d dur=%.2fs", len(routes_data["api_routes"]), len(routes_data["page_routes"]), time.monotonic() - t0)
    return {
        "routes":         all_routes,
        "api_routes":     [r["path"] for r in routes_data["api_routes"]],
        "server_actions": routes_data["server_actions"],
        "middleware":     [m["path"] for m in routes_data["middleware"]],
        "auth_paths":     routes_data["auth_paths"],
        "written_to":     written,
    }


# ── Dependencies ───────────────────────────────────────────────────────────────

@app.post("/dependencies")
async def dependencies(req: DependenciesRequest):
    """
    Map all imports and dependencies. Purely deterministic — no model call.
    Writes: /output/dependencies-{slug}.md
    """
    t0 = time.monotonic()
    log.debug("POST /dependencies path=%s", req.path or "/")
    dep_data = map_dependencies(req.path)
    written  = mw.write_dependencies(req.path, dep_data)
    asyncio.create_task(supabase_vector.store_artifact(written))
    log.debug("POST /dependencies done dur=%.2fs", time.monotonic() - t0)

    return {
        "path":       req.path,
        "internal":   dep_data["internal"],
        "external":   dep_data["external"],
        "graph":      dep_data["graph"],
        "written_to": written,
    }


# ── Summarize ──────────────────────────────────────────────────────────────────

@app.post("/summarize")
async def summarize(req: SummarizeRequest):
    """
    Summarize a single file: purpose, deps, risks, architectural notes.
    Writes: /output/summary-{slug}.md
    """
    t0 = time.monotonic()
    log.debug("POST /summarize file=%s", req.file)
    f = safe_resolve(req.file)
    if not f.exists() or not f.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {req.file!r}")

    content = read_file(f)
    if not content.strip():
        raise HTTPException(status_code=422, detail="File is empty or unreadable")

    det_deps = extract_imports(content)

    prompt = f"""Analyze this file. Respond with JSON only — no markdown fences, no explanation.

File: {req.file}
{content[:config.MAX_TOTAL_CHARS]}

Respond with exactly this structure:
{{"purpose":"one sentence: what this file does","dependencies":["dep1","dep2"],"risks":["risk1"],"architectural_notes":["note1"]}}"""

    raw    = await ollama_client.generate(prompt)
    parsed = ollama_client.parse_json_response(raw)

    purpose    = parsed.get("purpose",               raw[:300] if not parsed else "")
    deps       = parsed.get("dependencies",           det_deps)
    risks      = parsed.get("risks",                  [])
    arch_notes = parsed.get("architectural_notes",    [])

    written = mw.write_summary(req.file, purpose, deps[:30], risks, arch_notes)

    asyncio.create_task(supabase_vector.store_artifact(written))
    log.debug("POST /summarize done dur=%.2fs", time.monotonic() - t0)
    return {
        "file":               req.file,
        "purpose":            purpose,
        "dependencies":       deps[:30],
        "risks":              risks,
        "architectural_notes": arch_notes,
        "written_to":         written,
    }


# ── Context ────────────────────────────────────────────────────────────────────

@app.post("/context")
async def context(req: ContextRequest):
    """
    Build a full context bundle for a specific task.

    Order: scan → grep → vector search (nomic) → synthesis (qwen).
    Note: vector search and synthesis use different models.
    With MAX_LOADED_MODELS=1, there will be one model swap (~5-10s).

    Writes: /output/context-bundle.md
    """
    t0 = time.monotonic()
    log.debug("POST /context task=%r paths=%s", req.task, req.paths)
    try:
        result = await build_context(
            task=req.task,
            paths=req.paths,
            focus=req.focus,
            use_vector=req.use_vector,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    written = mw.write_context(
        task=req.task,
        files=result["files"],
        summary=result["summary"],
        risks=result["risks"],
        suggested_files=result["suggested_files"],
        vector_hits=result["vector_hits"],
        audit_table=result.get("audit_table", []),
        warnings=result.get("warnings", []),
        dropped_candidates=result.get("dropped_candidates", []),
    )
    markdown_content = ""
    try:
        markdown_content = Path(written).read_text(encoding="utf-8")
    except Exception:
        pass

    response_payload = {
        "task":               req.task,
        "files":              result["files"],
        "summary":            result["summary"],
        "risks":              result["risks"],
        "suggested_files":    result["suggested_files"],
        "vector_hits":        [h.get("path", "") for h in result["vector_hits"]],
        "audit_table":        result.get("audit_table", []),
        "warnings":           result.get("warnings", []),
        "dropped_candidates": result.get("dropped_candidates", []),
        "scope":              result.get("scope", []),
        "written_to":         written,
    }
    artifacts = artifact_store.write_record(
        tool="context",
        request=req.model_dump(),
        response=response_payload,
        markdown=markdown_content or None,
    )
    response_payload["artifacts"] = artifacts

    asyncio.create_task(supabase_vector.store_artifact(written))
    log.debug("POST /context done files=%d vector_hits=%d dur=%.2fs", len(result["files"]), len(result["vector_hits"]), time.monotonic() - t0)
    return response_payload


async def _run_issue_audit_background(
    run_id: str,
    req: IssueAuditRequest,
    scoped_paths: list[str],
    task: str,
) -> None:
    """Background worker for /agents/issue-auditor/run. Overwrites the pending record when done."""
    try:
        result = await build_context(
            task=task,
            paths=scoped_paths,
            focus=req.focus + req.requirements,
            use_vector=req.use_vector,
            force_issue_audit=True,
        )
        evidence_matrix = collect_agent_evidence(scoped_paths)
        deterministic_findings = findings_from_evidence(evidence_matrix, _issue_numbers(task, req.focus))

        written = mw.write_context(
            task=task,
            files=result["files"],
            summary=result["summary"],
            risks=result["risks"],
            suggested_files=result["suggested_files"],
            vector_hits=result["vector_hits"],
            audit_table=deterministic_findings or result.get("audit_table", []),
            warnings=result.get("warnings", []),
            dropped_candidates=result.get("dropped_candidates", []),
        )
        markdown_content = ""
        try:
            markdown_content = Path(written).read_text(encoding="utf-8")
        except Exception:
            pass

        if deterministic_findings:
            findings = deterministic_findings
        elif scoped_paths:
            findings = insufficient_evidence_findings(_issue_numbers(task, req.focus))
        else:
            findings = result.get("audit_table", [])
        run_status = "complete" if findings else "insufficient_evidence"
        warnings = list(result.get("warnings", []))

        response_payload = {
            "run_id":          run_id,
            "status":          run_status,
            "findings":        findings,
            "evidence_matrix": evidence_matrix,
            "suggested_files": result["suggested_files"],
            "warnings":        warnings,
        }
        artifacts = artifact_store.write_record(
            event_id=run_id,
            tool="agents/issue-auditor",
            request=req.model_dump(),
            response=response_payload,
            markdown=markdown_content or None,
            status=run_status,
        )

        # Await so upsert failures surface in the stored record's warnings
        vector_warnings = await supabase_vector.store_artifact_record(artifacts["record"], source_type="agent_run")
        if vector_warnings:
            warnings.extend(vector_warnings)
            response_payload["warnings"] = warnings
            artifact_store.write_record(
                event_id=run_id,
                tool="agents/issue-auditor",
                request=req.model_dump(),
                response=response_payload,
                markdown=markdown_content or None,
                status=run_status,
            )

        asyncio.create_task(supabase_vector.store_artifact(written))
        log.debug("issue-auditor background done run_id=%s findings=%d warnings=%d",
                  run_id, len(findings), len(warnings))
    except Exception:
        log.error("issue-auditor background error run_id=%s\n%s", run_id, traceback.format_exc())
        artifact_store.write_record(
            event_id=run_id,
            tool="agents/issue-auditor",
            request=req.model_dump(),
            response={"run_id": run_id, "status": "error", "findings": [], "evidence_matrix": [],
                      "suggested_files": [], "warnings": ["background task failed — check logs"]},
            status="error",
        )


@app.post("/agents/issue-auditor/run", response_model=IssueAuditResponse)
async def issue_auditor(req: IssueAuditRequest, background_tasks: BackgroundTasks):
    """
    Agentified issue audit workflow — returns immediately with run_id and status "running".

    Poll GET /agents/issue-auditor/status/{run_id} until status is no longer "running".
    The model call and evidence collection run in the background.
    Codex/Claude still owns final decisions and any GitHub writes.
    """
    scoped_paths = []
    for path in req.paths:
        clean = path.strip().strip("/")
        if clean.startswith(f"{req.repo.strip().strip('/')}/"):
            scoped_paths.append(clean)
        else:
            scoped_paths.append(f"{req.repo.strip().strip('/')}/{clean}")

    task = req.task
    if req.requirements:
        task += "\nRequirements:\n" + "\n".join(f"- {item}" for item in req.requirements)
    task += "\nReturn an issue audit table with issue, status, evidence_files, missing_evidence, recommendation."

    pending = artifact_store.write_pending_record(
        tool="agents/issue-auditor",
        request=req.model_dump(),
    )
    run_id = pending["event_id"]
    background_tasks.add_task(_run_issue_audit_background, run_id, req, scoped_paths, task)

    log.debug("POST /agents/issue-auditor/run queued run_id=%s", run_id)
    return IssueAuditResponse(
        run_id=run_id,
        status="running",
        findings=[],
        evidence_matrix=[],
        suggested_files=[],
        warnings=[],
        artifacts=pending,
    )


@app.get("/agents/issue-auditor/status/{run_id}")
async def issue_auditor_status(run_id: str):
    """
    Poll the status of a queued issue audit run.

    Returns the same shape as /agents/issue-auditor/run once complete.
    Status values: "running" | "complete" | "insufficient_evidence" | "error"
    """
    import json as _json
    if not all(c.isalnum() or c == "-" for c in run_id):
        raise HTTPException(status_code=400, detail="invalid run_id format")
    record_path = config.OUTPUT_DIR / "records" / f"{run_id}.json"
    if not record_path.exists():
        raise HTTPException(status_code=404, detail=f"run_id {run_id!r} not found")
    record = _json.loads(record_path.read_text(encoding="utf-8"))
    resp = dict(record.get("response") or {})
    resp["run_id"] = run_id
    resp["status"] = record.get("status", "unknown")
    resp.setdefault("findings",        [])
    resp.setdefault("evidence_matrix", [])
    resp.setdefault("suggested_files", [])
    resp.setdefault("warnings",        [])
    markdown_path = config.OUTPUT_DIR / "markdown" / f"{run_id}.md"
    resp["artifacts"] = {
        "event_id":  run_id,
        "record":    str(record_path),
        "markdown":  str(markdown_path) if markdown_path.exists() else None,
        "event_log": str(config.OUTPUT_DIR / "events.jsonl"),
    }
    return resp


# ── Diff Summary ───────────────────────────────────────────────────────────────

@app.post("/diff-summary")
async def diff_summary(req: DiffRequest):
    """
    Summarize a git diff: changes, risks, test recommendations.
    Writes: /output/diff-{hash}.md
    Always writes an artifact — including on model error or timeout.
    """
    t0 = time.monotonic()
    log.debug("POST /diff-summary diff_len=%d", len(req.diff))
    result = await review_diff(req.diff)

    t_artifact = time.monotonic()
    written = mw.write_diff(
        summary=result["summary"],
        risks=result["risks"],
        files_touched=result["files_touched"],
        test_recs=result["test_recommendations"],
    )
    artifact_ms = int((time.monotonic() - t_artifact) * 1000)

    timing = {**result["timing_ms"], "artifact_write": artifact_ms, "total": int((time.monotonic() - t0) * 1000)}

    if result["model_error"]:
        log.warning("POST /diff-summary model_error diff_len=%d model_ms=%d written=%s",
                    len(req.diff), result["timing_ms"]["model"], written)
    else:
        log.debug("POST /diff-summary done files=%d model_ms=%d total_ms=%d written=%s",
                  len(result["files_touched"]), result["timing_ms"]["model"], timing["total"], written)

    asyncio.create_task(supabase_vector.store_artifact(written))
    return {
        "summary":              result["summary"],
        "risks":                result["risks"],
        "files_touched":        result["files_touched"],
        "test_recommendations": result["test_recommendations"],
        "model_error":          result["model_error"],
        "timing_ms":            timing,
        "written_to":           written,
    }


# ── Vector Search ──────────────────────────────────────────────────────────────

@app.post("/vector-search")
async def vector_search(req: VectorSearchRequest):
    """
    Semantic vector search via Supabase pgvector.
    Embeds query using nomic-embed-text, searches existing index.
    Degrades gracefully if vectors not available.
    Writes: /output/vector-{slug}.md
    """
    t0 = time.monotonic()
    log.debug("POST /vector-search query=%r limit=%d", req.query, req.limit)
    available = await supabase_vector.is_available()

    if not available:
        log.debug("POST /vector-search skipped — vector store not available")
        return {
            "query":      req.query,
            "matches":    [],
            "written_to": "",
            "available":  False,
        }

    embedding = await ollama_client.embed(req.query)
    if embedding is None:
        log.warning("POST /vector-search embed failed query=%r", req.query)
        return {
            "query":      req.query,
            "matches":    [],
            "written_to": "",
            "available":  False,
        }

    matches  = await supabase_vector.search(embedding, limit=req.limit, threshold=req.threshold)
    written  = mw.write_vector_results(req.query, matches)

    log.debug("POST /vector-search done matches=%d dur=%.2fs", len(matches), time.monotonic() - t0)
    return {
        "query":      req.query,
        "matches":    matches,
        "written_to": written,
        "available":  True,
    }


# ── Index ──────────────────────────────────────────────────────────────────────

@app.post("/index")
async def index(req: IndexRequest):
    """
    Index code chunks into Supabase pgvector.

    ONLY uses nomic-embed-text (embed model). No qwen calls.
    This keeps memory pressure low and avoids model swaps during indexing.

    Run this before a coding session, not during one.
    On 8GB RAM CPU-only: ~1-2s per file. 80 files ≈ 2-3 minutes.
    """
    available = await supabase_vector.is_available()
    if not available:
        log.warning("POST /index skipped — vector store not available (migration not run?)")
        return {
            "paths":      req.paths,
            "indexed":    0,
            "skipped":    0,
            "errors":     0,
            "available":  False,
            "reason":     "vector store not ready — run the Supabase migration (supabase/migrations/) to create the code_embeddings table, then retry",
            "written_to": "",
        }

    indexed = 0
    skipped = 0
    errors  = 0

    for path in req.paths:
        try:
            base  = safe_resolve(path) if path and path != "." else config.REPO_ROOT
            files = walk_repo(base)

            for f in files[:config.MAX_FILES_PER_SCAN]:
                content = read_file(f)
                if not content.strip():
                    continue

                rp = rel_path(f)

                # Chunk into ~500-char pieces with 50-char overlap
                chunks = _chunk_text(content, chunk_size=500, overlap=50)

                for chunk in chunks:
                    if not chunk.strip():
                        continue

                    h = supabase_vector.chunk_hash(rp, chunk)

                    if not req.force and await supabase_vector.already_indexed(h):
                        skipped += 1
                        continue

                    embedding = await ollama_client.embed(f"{rp}\n{chunk}")
                    if embedding is None:
                        errors += 1
                        continue

                    ok = await supabase_vector.upsert_chunk(rp, chunk, embedding)
                    if ok:
                        indexed += 1
                    else:
                        errors += 1

        except Exception as e:
            log.warning("index error path=%s: %s", path, e)
            errors += 1

    log.debug("POST /index done indexed=%d skipped=%d errors=%d", indexed, skipped, errors)
    written = mw.write_index_report(req.paths, indexed, skipped, errors)

    return {
        "paths":      req.paths,
        "indexed":    indexed,
        "skipped":    skipped,
        "errors":     errors,
        "available":  True,
        "written_to": written,
    }


# ── Draft ──────────────────────────────────────────────────────────────────────

@app.post("/draft", response_model=DraftResponse)
async def draft(req: DraftRequest):
    """
    Two-tier agent: Claude (SR dev) plans → qwen (JR dev) generates → Claude reviews and applies.

    Reads the target file + any context_files, builds a prompt, and generates
    code using qwen2.5-coder. Returns the draft as text — Claude owns all writes.

    mode="create" → generate a new file from scratch
    mode="edit"   → read the existing file and apply the described change
    """
    t0 = time.monotonic()
    log.debug("POST /draft file=%s mode=%s task=%r", req.file, req.mode, req.task)

    # Read target file (if editing an existing one)
    existing_content = ""
    if req.mode == "edit":
        try:
            f = safe_resolve(req.file)
            if f.exists() and f.is_file():
                existing_content = read_file(f)
        except Exception as e:
            log.debug("draft: could not read target file %s: %s", req.file, e)

    # Read context files
    context_blocks: list[str] = []
    for cf in req.context_files[:5]:
        try:
            f = safe_resolve(cf)
            content = read_file(f)
            if content:
                context_blocks.append(f"// {cf}\n{content[:2000]}")
        except Exception:
            pass

    context_section = "\n\n".join(context_blocks)

    if req.mode == "edit" and existing_content:
        prompt = f"""Task: {req.task}
File: {req.file}
{("References:" + chr(10) + context_section) if context_section else ""}
Existing:
{existing_content[:config.MAX_TOTAL_CHARS]}

Return the complete updated file only. No explanation."""
    else:
        prompt = f"""Task: {req.task}
File: {req.file}
{("References:" + chr(10) + context_section) if context_section else ""}

Return the complete new file only. No explanation."""

    code = await ollama_client.generate(prompt)

    # Strip accidental markdown fences the model may add despite instructions
    import re as _re
    code = _re.sub(r"^```[a-z]*\n?", "", code.strip(), flags=_re.MULTILINE)
    code = _re.sub(r"\n?```$", "", code.strip(), flags=_re.MULTILINE)

    written = mw.write_draft(file=req.file, task=req.task, mode=req.mode, code=code)

    asyncio.create_task(supabase_vector.store_artifact(written))
    log.debug("POST /draft done mode=%s code_len=%d dur=%.2fs", req.mode, len(code), time.monotonic() - t0)
    return DraftResponse(file=req.file, mode=req.mode, code=code, written_to=written)


# ── Scaffold ───────────────────────────────────────────────────────────────────

@app.post("/scaffold", response_model=ScaffoldResponse)
async def scaffold(req: ScaffoldRequest):
    """
    Multi-file code generation for context window preservation.

    Use when Claude has broken a feature into files and wants to delegate
    the mechanical generation of each one — preserving its own context window
    for orchestration, review, and decisions rather than typing.

    Each file is generated sequentially (memory constraint — one model at a time).
    Claude reviews the full batch via the artifacts dir scaffold-*.md before applying anything.
    Claude owns all writes.
    """
    t0 = time.monotonic()
    log.debug("POST /scaffold task=%r files=%d", req.task, len(req.files))

    # Read shared context files once — passed to every generation
    shared_context_blocks: list[str] = []
    for cf in req.context_files[:5]:
        try:
            f = safe_resolve(cf)
            content = read_file(f)
            if content:
                shared_context_blocks.append(f"// {cf}\n{content[:1500]}")
        except Exception:
            pass
    shared_context = "\n\n".join(shared_context_blocks)

    results: list[ScaffoldFileResult] = []
    errors: list[str] = []

    for sf in req.files:
        try:
            existing_content = ""
            if sf.mode == "edit":
                try:
                    f = safe_resolve(sf.file)
                    if f.exists():
                        existing_content = read_file(f)
                except Exception:
                    pass

            if sf.mode == "edit" and existing_content:
                prompt = f"""Task: {req.task}
File job: {sf.spec}
File: {sf.file}
{("References:" + chr(10) + shared_context) if shared_context else ""}
Existing:
{existing_content[:config.MAX_TOTAL_CHARS]}

Return the complete updated file only. No explanation."""
            else:
                prompt = f"""Task: {req.task}
File job: {sf.spec}
File: {sf.file}
{("References:" + chr(10) + shared_context) if shared_context else ""}

Return the complete new file only. No explanation."""

            code = await ollama_client.generate(prompt)

            import re as _re
            code = _re.sub(r"^```[a-z]*\n?", "", code.strip(), flags=_re.MULTILINE)
            code = _re.sub(r"\n?```$", "", code.strip(), flags=_re.MULTILINE)

            written = mw.write_scaffold_file(
                file=sf.file, task=req.task, spec=sf.spec, mode=sf.mode, code=code
            )
            asyncio.create_task(supabase_vector.store_artifact(written))
            results.append(ScaffoldFileResult(file=sf.file, mode=sf.mode, code=code, written_to=written))
            log.debug("scaffold file=%s done code_len=%d", sf.file, len(code))

        except Exception as e:
            log.error("scaffold file=%s error: %s", sf.file, e)
            errors.append(f"{sf.file}: {e}")

    log.debug("POST /scaffold done files=%d errors=%d dur=%.2fs", len(results), len(errors), time.monotonic() - t0)
    return ScaffoldResponse(task=req.task, files=results, total=len(results), errors=errors)


# ── Generic Agent Run ──────────────────────────────────────────────────────────

@app.post("/tools/call")
async def tools_call(req: ToolCallRequest):
    """
    Execute a single tool by name. Used by the MCP wrapper and any caller that
    wants direct tool access without going through the full agent loop.

    Available tools: scan_directory, find_in_code, read_file, grep, health_check, search_memory, update_plan
    Returns: {"name": str, "result": str, "ok": bool, "error_type": str | null}
    """
    result = await tool_registry.execute_tool(req.name, req.arguments)
    return {"name": req.name, "result": result.data, "ok": result.ok, "error_type": result.error_type}


@app.get("/agents/tools")
async def agents_tools():
    """
    Tool manifest — returns JSON schemas and metadata for all tools the agent can call.
    Any calling agent (Claude, Codex, Qwen, etc.) hits this to discover capabilities.
    Schema format is OpenAI/Ollama/MCP-compatible.
    Each entry includes 'scopes' and 'side_effects' from ToolMeta.
    """
    tools = []
    for schema in tool_registry.get_tool_definitions():
        name = schema["function"]["name"]
        meta = tool_registry.get_tool_metadata(name)
        tools.append({
            **schema,
            "scopes":       meta.scopes if meta else [],
            "side_effects": meta.side_effects if meta else False,
        })
    return {"tools": tools}


async def _run_agent_background(run_id: str, req: AgentRunRequest) -> None:
    """Background worker for POST /agents/run."""
    try:
        result = await agent_runner.run_agent(
            task=req.task,
            tools=req.tools or None,
            system_prompt=req.system_prompt,
            max_iterations=req.max_iterations,
            allowed_scopes=req.allowed_scopes,
        )
        written = mw.write_agent_run(
            task=req.task,
            final_answer=result.final_answer,
            tool_calls_made=result.tool_calls_made,
            iterations=result.iterations,
            stopped_reason=result.stopped_reason,
            warnings=[],
        )
        markdown_content = ""
        try:
            markdown_content = Path(written).read_text(encoding="utf-8")
        except Exception:
            pass

        response_payload = {
            "run_id":               run_id,
            "status":               "complete" if result.stopped_reason == "final_answer" else result.stopped_reason,
            "task":                 req.task,
            "final_answer":         result.final_answer,
            "tool_calls_made":      result.tool_calls_made,
            "iterations":           result.iterations,
            "stopped_reason":       result.stopped_reason,
            "warnings":             [],
            "memory_context_used":  result.memory_context_used,
            "memory_hits":          result.memory_hits,
            "plan_state":           result.plan_state,
        }
        artifacts = artifact_store.write_record(
            event_id=run_id,
            tool="agents/run",
            request=req.model_dump(),
            response=response_payload,
            markdown=markdown_content or None,
            status=response_payload["status"],
        )
        response_payload["artifacts"] = artifacts

        vector_warnings = await supabase_vector.store_artifact_record(artifacts["record"], source_type="agent_run")
        if vector_warnings:
            response_payload["warnings"] = vector_warnings
            artifact_store.write_record(
                event_id=run_id,
                tool="agents/run",
                request=req.model_dump(),
                response=response_payload,
                markdown=markdown_content or None,
                status=response_payload["status"],
            )

        asyncio.create_task(supabase_vector.store_artifact(written))
        log.debug("agent/run background done run_id=%s stopped=%s iter=%d tool_calls=%d",
                  run_id, result.stopped_reason, result.iterations, len(result.tool_calls_made))
    except Exception:
        log.error("agent/run background error run_id=%s\n%s", run_id, traceback.format_exc())
        artifact_store.write_record(
            event_id=run_id,
            tool="agents/run",
            request=req.model_dump(),
            response={"run_id": run_id, "status": "error", "task": req.task,
                      "final_answer": "", "tool_calls_made": [], "iterations": 0,
                      "stopped_reason": "error", "warnings": ["background task failed — check logs"]},
            status="error",
        )


@app.post("/agents/run", response_model=AgentRunResponse)
async def agents_run(req: AgentRunRequest, background_tasks: BackgroundTasks):
    """
    Delegate any task to the local junior agent.

    The agent autonomously decides what files to read/scan/grep, loops until it has
    enough evidence, and returns a final answer with a full tool-call trace.

    Returns immediately with run_id and status "running".
    Poll GET /agents/run/status/{run_id} until status is no longer "running".

    Body:
      task            — what to accomplish (required)
      tools           — tool names to enable; empty = all tools
      max_iterations  — default 10
      system_prompt   — override the default system prompt

    GET /agents/tools to see available tool definitions and schemas.
    """
    pending = artifact_store.write_pending_record(
        tool="agents/run",
        request=req.model_dump(),
    )
    run_id = pending["event_id"]
    background_tasks.add_task(_run_agent_background, run_id, req)

    log.debug("POST /agents/run queued run_id=%s task_len=%d", run_id, len(req.task))
    return AgentRunResponse(
        run_id=run_id,
        status="running",
        task=req.task,
        artifacts=pending,
    )


@app.get("/agents/run/status/{run_id}")
async def agents_run_status(run_id: str):
    """
    Poll the status of a queued agent run.

    Same shape as POST /agents/run once complete.
    Status values: "running" | "complete" | "final_answer" | "max_iterations" | "timeout" | "model_error" | "error"
    """
    import json as _json
    if not all(c.isalnum() or c == "-" for c in run_id):
        raise HTTPException(status_code=400, detail="invalid run_id format")
    record_path = config.OUTPUT_DIR / "records" / f"{run_id}.json"
    if not record_path.exists():
        raise HTTPException(status_code=404, detail=f"run_id {run_id!r} not found")
    record = _json.loads(record_path.read_text(encoding="utf-8"))
    resp = dict(record.get("response") or {})
    resp["run_id"]         = run_id
    resp["status"]         = record.get("status", "unknown")
    resp.setdefault("task",            "")
    resp.setdefault("final_answer",    "")
    resp.setdefault("tool_calls_made", [])
    resp.setdefault("iterations",      0)
    resp.setdefault("stopped_reason",  "")
    resp.setdefault("warnings",        [])
    markdown_path = config.OUTPUT_DIR / "markdown" / f"{run_id}.md"
    resp["artifacts"] = {
        "event_id":  run_id,
        "record":    str(record_path),
        "markdown":  str(markdown_path) if markdown_path.exists() else None,
        "event_log": str(config.OUTPUT_DIR / "events.jsonl"),
    }
    return resp


def _chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """Split text into overlapping chunks."""
    chunks = []
    start  = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks
