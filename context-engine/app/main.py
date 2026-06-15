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
    log.info("  agent_model=%s", config.OLLAMA_AGENT_MODEL)
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
    LogLevelRequest, LogLevelResponse,
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
    agent_ok       = any(config.OLLAMA_AGENT_MODEL  in m for m in models)
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
        "agent_model":            config.OLLAMA_AGENT_MODEL,
        "agent_model_available":  agent_ok,
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
    mcp_url  = "http://127.0.0.1:8089/mcp"

    reason_ok  = any(config.OLLAMA_REASON_MODEL in m for m in models)
    agent_ok   = any(config.OLLAMA_AGENT_MODEL in m for m in models)
    gen_status    = "available" if model_ok  else "OFFLINE — skip /draft /scaffold /scan /find /summarize"
    reason_status = "available" if reason_ok else "OFFLINE — /context and /diff-summary will degrade"
    agent_status  = "available" if agent_ok else "OFFLINE — /agents/run cannot delegate"
    emb_status    = "available" if embed_ok  else "OFFLINE — skip /index and /vector-search"
    vec_status    = "ready" if vec_ok else "not indexed — run /index first; /vector-search returns empty until then"

    return (
        "# context-engine — Agent Integration Protocol\n"
        f"_repo: `{repo}` · {base}_\n"
        "\n---\n\n"

        "## Caller contract\n\n"
        f"Junior dev available at `{base}`.\n"
        f"Check: `GET /healthcheck` → `{{\"ok\": true}}`\n"
        f"Troubleshoot: `GET {base}/debug` — model state, vector row count, config. No model calls.\n\n"
        "**Preferred transport: MCP. REST remains available for scripts, compatibility, and troubleshooting.**\n"
        "**SR/JR pattern:** you plan, specify, review, apply. The junior investigates and returns evidence.\n"
        "**The junior is read-only** — it never writes to the repo.\n\n"

        "### Configure the MCP server\n\n"
        f"The persistent Streamable HTTP MCP service runs at `{mcp_url}` and forwards to REST. "
        "It contains no planning, verification, scanning, or agent loop logic.\n\n"
        "MCP is a control plane: large outputs are written to artifacts and MCP returns "
        "artifact references plus concise summaries by default. Pass `mode=\"inline\"` only "
        "when the full payload should enter the active conversation; use `mode=\"summary\"` "
        "to force artifact-reference responses. REST responses remain unchanged.\n\n"
        "**Install/restart the launchd service:**\n"
        "```bash\n"
        "bash scripts/install-mcp.sh\n"
        "```\n\n"
        "**Claude Code:**\n"
        "```bash\n"
        f"claude mcp add --scope user --transport http context-engine {mcp_url}\n"
        "```\n\n"
        "**Codex:**\n"
        "```bash\n"
        f"codex mcp add context-engine --url {mcp_url}\n"
        "```\n\n"
        "The MCP launchd service is separate from the REST launchd service. Restarting MCP does not "
        "restart the Context Engine Agent.\n\n"

        "**Live status:**\n\n"
        "| Capability | Status |\n"
        "|---|---|\n"
        f"| Reasoning model (`{config.OLLAMA_REASON_MODEL}`) | {reason_status} |\n"
        f"| Agent model (`{config.OLLAMA_AGENT_MODEL}`) | {agent_status} |\n"
        f"| Code model (`{config.OLLAMA_MODEL}`) | {gen_status} |\n"
        f"| Embeddings (`{config.OLLAMA_EMBED_MODEL}`) | {emb_status} |\n"
        f"| Vector index | {vec_status} |\n\n"

        "**MCP decision table:**\n\n"
        "| Situation | MCP tool |\n"
        "|---|---|\n"
        "| Repository investigation, architecture question, or multi-file evidence gathering | `investigate_codebase` — primary/default |\n"
        "| Gather a bounded pre-task context bundle | `load_context` |\n"
        "| Review changes after editing | `review_diff` |\n"
        "| Audit an issue against repository evidence | `audit_issue` |\n"
        "| One bounded primitive retrieval | Advanced tools: `scan_directory`, `find_in_code`, `summarize_file`, `dependency_analysis`, `vector_search` |\n"
        "| Novel architecture, security, complex logic | Senior engineer owns the decision; delegate only bounded evidence gathering |\n"
        "| Engine unreachable | Proceed without it — never block on the scout |\n\n"

        "**Delegation rule:** prefer `investigate_codebase` over manually chaining advanced MCP tools. "
        "The high-level tool invokes the existing Context Engine Agent, which owns planning, repository "
        "tool selection, memory search, verification, repair passes, and evidence trails.\n\n"

        f"**Path convention:** `REPO_ROOT` is `{repo}`. All `path` values must use `<owner>/<repo>/` prefix — "
        "e.g. `\"ascendvent/checkin-ascendvent/src\"`. Never use bare `\".\"` — it scans all of `~/Repos`.\n\n"
        f"**Skip rule:** one-liner task, files already in context, or `{base}/healthcheck` returns non-200 — state reason explicitly.\n\n"
        "**Output files:** `~/Library/Application Support/context-store/artifacts/` — "
        "always verify actual source files before editing; output files are scout reports, not ground truth.\n\n"

        "**MCP tool reference:**\n\n"
        "- `investigate_codebase(task, tools?, max_iterations?, system_prompt?, allowed_scopes?)` "
        "calls `/agents/run`, polls status, and returns a summary/reference by default when large.\n"
        "- `load_context(task, paths?, focus?, use_vector?, mode?)` calls `/context`.\n"
        "- `review_diff(diff, mode?)` calls `/diff-summary`.\n"
        "- `audit_issue(task, repo, paths, focus?, requirements?, use_vector?, mode?)` calls the async issue auditor and polls it.\n"
        "- Advanced direct tools map one-to-one to `/scan`, `/find`, `/summarize`, `/dependencies`, `/routes`, `/draft`, `/scaffold`, and `/vector-search`.\n"
        "- Large MCP responses include `artifact_id`, `artifact_path`, `artifact_type`, `summary`, `token_estimate`, and `metadata`.\n\n"

        "**REST endpoint reference (compatibility):**\n\n"

        "**POST /agents/run** — delegate any task; returns `run_id` immediately, poll until `status != \"running\"`.\n"
        "```json\n"
        '{  "task": "Find all route handlers in ryemyster/context-manager/context-engine/app",\n'
        '   "tools": [],  "max_iterations": 10,  "allowed_scopes": null  }\n'
        "```\n"
        "- `allowed_scopes`: `null` = all; restrict with `[\"repo:read\"]`, `[\"memory:read\"]`, `[\"engine:read\"]`; `update_plan` always available.\n"
        "Poll response fields:\n"
        "- `final_answer`, `tool_calls_made` `{name, arguments, result}`, `iterations`\n"
        "- `stopped_reason`: `\"final_answer\"` | `\"max_iterations\"` | `\"timeout\"` | `\"model_error\"` | `\"verification_failed\"`\n"
        "- `memory_context_used` (bool), `memory_hits` (int)\n"
        "- `plan_state` — last `update_plan` call: `{goal, steps, current_step, blockers}`; `{}` if never called\n"
        "- `verification` — post-hoc coherence check: `{passed: bool|null, rationale: str, unsupported_claims: [str], evidence_gap: bool, repaired?: bool}`;\n"
        "  populated when `stopped_reason == \"final_answer\"` or `\"verification_failed\"`; `{}` on timeout/max_iterations/model_error;\n"
        "  `repaired: true` means a repair pass fired and re-verified; "
        "`{passed: null, error: \"verifier_timeout\"}` means the answer is returned but verification exceeded its ceiling\n\n"
        "**Agent latency boundaries:**\n"
        f"- Memory preflight is best-effort and capped at `{config.OLLAMA_AGENT_MEMORY_TIMEOUT:g}s`.\n"
        f"- Each native tool-calling turn is capped at `{config.OLLAMA_AGENT_CALL_TIMEOUT:g}s`.\n"
        f"- Structured next-action selection is capped at `{config.OLLAMA_AGENT_SELECT_TIMEOUT:g}s`.\n"
        f"- Post-run verification is capped at `{config.OLLAMA_AGENT_VERIFY_TIMEOUT:g}s` and degrades without discarding the answer.\n"
        f"- The complete agent run budget is `{config.OLLAMA_AGENT_TIMEOUT:g}s`; the outer worker guard is defensive only.\n\n"
        "**Tool-call reliability:** the agent prompt is generated from the tools enabled for that run, so it never "
        "orders calls to unavailable tools. A response cannot become a final answer before an enabled tool returns evidence. "
        "The primary loop uses a constrained JSON action schema to choose either one enabled tool or a final answer from accumulated "
        "evidence. Absolute paths under `REPO_ROOT` are normalized to the required repository-relative contract before execution; "
        "external absolute paths remain rejected. Repeating an identical successful tool call triggers schema-constrained answer "
        "synthesis from existing evidence. Native `tool_calls` and valid JSON tool calls embedded in text remain recovery paths.\n\n"
        "Expected tool call order: `search_memory → update_plan → scan_directory → find_in_code or grep → read_file`\n\n"

        "**GET /agents/tools** — tool manifest (schemas + `scopes` + `side_effects`). "
        "Tools: `scan_directory`, `find_in_code`, `read_file`, `grep`, `health_check`, `search_memory`, `update_plan`.\n\n"

        "**POST /context** — scan + grep + optional vector + synthesis. "
        '`{"task": "...", "paths": ["owner/repo/src"], "focus": ["term"]}` → `context-bundle.md`\n\n'

        "**POST /agents/issue-auditor/run** — async evidence-based issue triage; same async/poll pattern. "
        "Status: `complete` | `insufficient_evidence` | `error`.\n\n"

        "**POST /scan** — "
        '`{"path": "owner/repo/src/app/api"}` → file list + patterns · `scan-<slug>.md`\n\n'

        "**POST /find** — "
        '`{"query": "rate limiting", "path": "owner/repo/src"}` → matches + synthesis · `find-<slug>.md`\n\n'

        "**POST /summarize** — "
        '`{"file": "owner/repo/src/app/api/checkins/route.ts"}` → purpose, deps, risks · `summary-<slug>.md`\n\n'

        "**POST /routes** — "
        '`{"path": "owner/repo"}` → api_routes, page_routes, middleware · `routes.md`\n\n'

        "**POST /dependencies** — "
        '`{"path": "owner/repo/src/lib"}` → import graph · `dependencies-<slug>.md`\n\n'

        "**POST /diff-summary** — "
        f'`{{"diff": "<git diff output>"}}` → summary, risks, test_recommendations · `diff-<hash>.md` '
        f"(paginate above ~{config.DIFF_MAX_CHARS:,} chars)\n\n"

        "**POST /vector-search** — "
        '`{"query": "auth session middleware", "limit": 8}` → ranked chunks · `vector-<slug>.md` (requires `/index`)\n\n'

        "**POST /index** — "
        '`{"paths": ["owner/repo/src"], "force": false}` — run once per session when code has changed\n\n'

        "**POST /draft** — single-file JR dev delegation. "
        '`{"task": "...", "file": "owner/repo/src/lib/types.ts", "context_files": [...], "mode": "edit"}` → `draft-<slug>.md`\n\n'

        "**POST /scaffold** — multi-file delegation to preserve context window. "
        '`{"task": "...", "files": [{"file": "...", "spec": "...", "mode": "create"}], "context_files": [...]}` → `scaffold-<slug>.md` per file\n\n'

        "**GET /log-level** — return current log level: `{\"previous\": \"WARNING\", \"current\": \"WARNING\"}`\n\n"
        "**POST /log-level** — change log level at runtime without a restart. "
        '`{"level": "DEBUG"}` → `{"previous": "WARNING", "current": "DEBUG"}`. '
        "Valid levels: TRACE | DEBUG | INFO | WARNING | ERROR. Changes are in-memory only — reverts on restart.\n\n"

        "\n---\n\n"

        "## Implementation details\n"
        "> Not intended for agent rule files. Changes here do not affect caller behaviour.\n\n"

        "### Model routing\n\n"
        "| Model | Role | Called by |\n"
        "|-------|------|-----------|\n"
        f"| `{config.OLLAMA_REASON_MODEL}` | Reasoning — judgment, risks, what matters | `/diff-summary` |\n"
        f"| `{config.OLLAMA_AGENT_SELECT_MODEL}` | Agent selection — schema-constrained tool choice | `/agents/run` before evidence |\n"
        f"| `{config.OLLAMA_AGENT_MODEL}` | Agent answer — bounded final synthesis | `/agents/run` after evidence |\n"
        f"| `{config.OLLAMA_AGENT_VERIFY_MODEL}` | Agent verification — schema-constrained evidence check | `/agents/run` verifier |\n"
        f"| `{config.OLLAMA_MODEL}` | Code — pattern matching, generation, summarisation | `/context`, `/draft`, `/scaffold`, `/scan`, `/find`, `/summarize` |\n"
        f"| `{config.OLLAMA_EMBED_MODEL}` | Embeddings | `/index`, `/vector-search`, `/context`, agent artifact indexing |\n\n"
        "**Routing rules:**\n"
        "- `/diff-summary` uses `generate_reasoning()` with the 9B reasoning model.\n"
        "- `/agents/run` uses fast schema-constrained 3B selection and verification plus bounded 3B answer generation.\n"
        "- Agent instructions and call order are derived only from tools enabled for the current run.\n"
        "- `/context` uses the code model — relevance scoring is pattern matching, not reasoning.\n"
        "- `/draft` and `/scaffold` use the code model — generating code from a clear spec is pattern matching.\n"
        "- Native tool calling is an optional fallback, not the primary selection path.\n\n"

        "### Agent verifier + repair pass\n\n"
        "After every `final_answer` stop, `agent_runner._verify_answer()` calls the schema-constrained agent verifier with the task goal, "
        "tool evidence (capped at 1500 chars), and final answer. Returns `{passed, rationale, unsupported_claims, evidence_gap}`.\n"
        f"Verification is capped at {config.OLLAMA_AGENT_VERIFY_TIMEOUT:g}s. On timeout, the completed answer remains available "
        "with `{passed: null, error: \"verifier_timeout\"}`.\n"
        "If `passed is False`, `_build_repair_prompt()` injects the unsupported claims as a user message and `_execute_loop()` "
        "re-runs for up to `AGENT_MAX_REPAIR_ITERATIONS` (default 3) cycles. The result is re-verified; "
        "`verification[\"repaired\"] = True` marks that a repair occurred. "
        "If the repair pass also fails, `stopped_reason` becomes `\"verification_failed\"`.\n"
        "Skipped entirely on `max_iterations`, `timeout`, or `model_error` stops.\n\n"
        "**Tool result confirmation:** `scan_directory` and `read_file` return `error_type: \"needs_confirmation\"` with a `candidates` "
        "list when the requested path does not exist — the nearest existing ancestor's children are listed so the model can "
        "self-correct and retry. Other error types: `path_rejected`, `not_found`, `invalid_input`, `engine_down`, `scope_denied`.\n\n"

        "### Output flow\n\n"
        "`/context` and agent endpoints write a canonical JSON record, append to `events.jsonl`, render Markdown, "
        "then optionally embed the record with Ollama and upsert to Supabase. "
        "The vector store is a rebuildable index; local JSON artifacts are the durable source of truth.\n\n"

        "### Worked examples\n\n"
        "**MCP agent delegation — primary pattern:**\n"
        "```\n"
        "1. investigate_codebase(task=\"Determine whether authentication protects all agent endpoints\")\n"
        "2. Check tool_calls_made (non-empty = real exploration), plan_state.goal, verification.passed\n"
        "3. Verify source files before acting on final_answer\n"
        "```\n\n"
        "The MCP adapter performs the REST start/poll sequence internally. REST callers may continue "
        "using `POST /agents/run` and `GET /agents/run/status/{run_id}` unchanged.\n\n"
        "**Single file delegation (/draft):**\n"
        "```\n"
        "1. POST /context  →  read context-bundle.md  →  find target files\n"
        "2. POST /summarize on target  →  understand current shape\n"
        "3. POST /draft (spec is clear + mechanical)  →  read draft-*.md  →  apply\n"
        "4. POST /diff-summary  →  read risks\n"
        "```\n\n"
        "**Issue audit:**\n"
        "```\n"
        "1. POST /agents/issue-auditor/run  →  {run_id, status: 'running'}\n"
        "2. GET /agents/issue-auditor/status/{run_id}  →  poll until done\n"
        "3. Read findings + artifacts.record\n"
        "4. Verify evidence files before closing or commenting on issues\n"
        "```\n\n"

        "### Adding to a project\n\n"
        "**Claude Code — add to `CLAUDE.md` or `.claude/rules/context-engine.md`:**\n"
        "```markdown\n"
        "## Context Engine\n\n"
        "Use the `context-engine` MCP server for non-trivial repository work.\n"
        "Prefer `investigate_codebase` for repository investigation; do not manually orchestrate "
        "`scan_directory`, `find_in_code`, or other advanced tools when delegation fits.\n"
        "Use `load_context` for bounded pre-task context and `review_diff` after edits.\n"
        "Context Engine is read-only. Verify its evidence and own all file writes and decisions.\n"
        "```\n\n"
        "**Codex CLI / Qwen Code — add to `~/.codex/AGENTS.md` or `AGENTS.md` in project root:**\n"
        "```markdown\n"
        "## Local Context Engine\n\n"
        "Use the `context-engine` MCP server as a read-only junior engineer.\n"
        "For non-trivial repository investigations, call `investigate_codebase` first and let the "
        "existing agent plan, scan, read, verify, and repair. Use advanced direct tools only for "
        "bounded primitive retrieval. You own architecture, review, and all file writes.\n\n"
        f"Live protocol and fallback REST details: GET {base}/setup\n"
        f"REPO_ROOT is `{repo}`. Every path must include `<owner>/<repo>/` prefix. Never bare `.`.\n\n"
        "If the MCP server or REST healthcheck is unavailable, continue without it.\n"
        "```\n"
        "\n---\n"
    )


# ── Log level ──────────────────────────────────────────────────────────────────

@app.get("/log-level", response_model=LogLevelResponse)
async def get_log_level():
    import logging
    current = logging.getLevelName(log.level)
    return LogLevelResponse(previous=current, current=current)


@app.post("/log-level", response_model=LogLevelResponse)
async def set_log_level(req: LogLevelRequest):
    import logging
    from .logger import TRACE
    previous = logging.getLevelName(log.level)
    level_map = {
        "TRACE":   TRACE,
        "DEBUG":   logging.DEBUG,
        "INFO":    logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR":   logging.ERROR,
    }
    new_level = level_map.get(req.level.upper())
    if new_level is None:
        raise HTTPException(status_code=400, detail=f"Unknown level '{req.level}'. Valid: TRACE DEBUG INFO WARNING ERROR")
    log.setLevel(new_level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).setLevel(new_level)
    log.warning("log level changed previous=%s current=%s", previous, req.level.upper())
    return LogLevelResponse(previous=previous, current=req.level.upper())


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
    # Defensive guard only. Individual model turns are bounded inside the agent.
    _wall_limit = config.OLLAMA_AGENT_TIMEOUT + config.OLLAMA_AGENT_CALL_TIMEOUT
    try:
        result = await asyncio.wait_for(
            agent_runner.run_agent(
                task=req.task,
                tools=req.tools or None,
                system_prompt=req.system_prompt,
                max_iterations=req.max_iterations,
                allowed_scopes=req.allowed_scopes,
            ),
            timeout=_wall_limit,
        )
    except asyncio.TimeoutError:
        log.error("agent/run background wall-clock timeout run_id=%s limit=%.0fs", run_id, _wall_limit)
        artifact_store.write_record(
            event_id=run_id,
            tool="agents/run",
            request=req.model_dump(),
            response={
                "run_id": run_id, "status": "timeout", "task": req.task,
                "final_answer": "[agent timed out — wall-clock limit exceeded]",
                "tool_calls_made": [], "iterations": 0,
                "stopped_reason": "timeout",
                "warnings": [f"wall-clock limit of {_wall_limit:.0f}s exceeded"],
            },
            status="timeout",
        )
        return
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
        return
    try:
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
            "verification":         result.verification,
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
