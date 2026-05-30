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

import time
import traceback
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from . import config
from . import ollama_client, supabase_vector
from .logger import log


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
from .models import (
    ScanRequest, FindRequest, DependenciesRequest, SummarizeRequest,
    ContextRequest, DiffRequest, VectorSearchRequest, IndexRequest, DraftRequest, ScaffoldRequest,
    HealthResponse, ScanResponse, FindResponse, RoutesResponse,
    DependenciesResponse, SummarizeResponse, ContextResponse,
    DiffResponse, VectorSearchResponse, IndexResponse, DraftResponse, ScaffoldResponse, ScaffoldFileResult,
)
from .repo_reader import safe_resolve, read_file, rel_path, walk_repo, build_snippet_block
from .search_worker import find_in_repo, extract_imports
from .route_extractor import extract_routes
from .dependency_mapper import map_dependencies
from .scanner import scan_directory
from .diff_reviewer import review_diff
from .context_builder import build_context

app = FastAPI(
    title="context-engine",
    version="1.0.0",
    description="Local AI context layer for Claude Code. Scout, not engineer.",
    lifespan=lifespan,
)


class _RequestLog(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        t0 = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            dur = time.monotonic() - t0
            log.error("%s %s unhandled %.3fs\n%s",
                      request.method, request.url.path, dur, traceback.format_exc())
            return JSONResponse(status_code=500, content={"detail": "internal server error"})
        dur = time.monotonic() - t0
        ms  = dur * 1000
        lvl = log.warning if (response.status_code >= 500 or ms > config.SLOW_REQUEST_MS) else log.info
        lvl("%s %s %d %.0fms", request.method, request.url.path, response.status_code, ms)
        return response


app.add_middleware(_RequestLog)


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
        "Three jobs, in priority order:\n\n"
        "1. **Retrieval** — finds relevant files and code without burning your token budget on recall\n"
        "2. **Reasoning synthesis** — uses a local reasoning model to judge what matters and what's risky\n"
        "3. **Code delegation** — lets you hand off mechanical code generation to a local code model\n\n"
        "You own all planning, architecture, security decisions, and every file write.\n\n"
        "**Hard constraint:** this service never writes to the repo. All output goes to `./ai-context/` as Markdown.\n\n"
        f"**Path convention:** `REPO_ROOT` is `{repo}`. All `path` parameters must be prefixed with "
        "`<owner>/<repo>/` — e.g. `\"ascendvent/checkin-ascendvent/src\"`. Never use bare `\".\"` — it scans all of `~/Repos`.\n"
        "\n---\n\n"

        "## Three-model stack\n\n"
        "| Model | Role | Called by |\n"
        "|-------|------|-----------|\n"
        f"| `{config.OLLAMA_REASON_MODEL}` | Reasoning — judgment, risks, what matters | `/diff-summary` |\n"
        f"| `{config.OLLAMA_MODEL}` | Code — pattern matching, generation, summarisation | `/context`, `/draft`, `/scaffold`, `/scan`, `/find`, `/summarize` |\n"
        f"| `{config.OLLAMA_EMBED_MODEL}` | Embeddings — semantic search only, no generation | `/index`, `/vector-search`, `/context` |\n\n"
        "**Why the split matters:**\n"
        "- `/diff-summary` uses the reasoning model — risk analysis and test recommendations are judgment.\n"
        "- `/context` uses the code model — relevance scoring is pattern matching, not reasoning. "
        "Also avoids a slow 9b cold-load after the embed step.\n"
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
        "| Novel architecture, security, complex logic | Claude only — do not delegate |\n"
        "| Engine unreachable | Proceed without it — never block on the scout |\n\n"
        f"**Skip when:** one-liner task, files already in context, or `{base}/healthcheck` returns non-200.\n\n"
        f"**Troubleshoot:** `GET {base}/debug` — model state, vector row count, config. No model calls.\n"
        "\n---\n\n"

        "## Endpoint reference\n\n"

        "### POST /context — use before every non-trivial task\n"
        "Model: reasoning. Scans paths, greps focus terms, runs vector search, synthesises what matters.\n\n"
        "```json\n"
        '{  "task": "add rate limiting to the check-in API",\n'
        '   "paths": ["ascendvent/checkin-ascendvent/src/app/api", "ascendvent/checkin-ascendvent/src/lib"],\n'
        '   "focus": ["rate-limit", "middleware", "checkins"]  }\n'
        "```\n"
        "Returns: `summary`, `suggested_files`, `risks`, `vector_hits`, `written_to` → read `context-bundle.md`\n\n"

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
        "Model: reasoning. `{\"diff\": \"<git diff>\"}` → summary, risks, test_recommendations · `diff-<hash>.md`\n"
        "Call after every set of edits: `git diff | ...`\n\n"

        "### POST /vector-search\n"
        'Model: embeddings. `{"query": "auth session middleware", "limit": 8}` → ranked chunks · `vector-<slug>.md`\n'
        "Requires `/index` to have been run. Check `vector_ready` in `/health` first.\n\n"

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
        "All output written to `./ai-context/` (gitignored). Read the file — don't just use the API response.\n\n"
        "| File | Endpoint | Re-use if |\n"
        "|---|---|---|\n"
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
        "\n---\n\n"

        "## Worked examples\n\n"
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
        "Add to the project's `CLAUDE.md`:\n\n"
        "```\n"
        "## Context Engine\n\n"
        f"Local context scout and code delegation layer at {base}.\n\n"
        "Before any non-trivial task: POST /context, read ./ai-context/context-bundle.md.\n"
        "For mechanical single-file work: POST /draft, review draft-*.md, apply manually.\n"
        "For multi-file features: POST /scaffold, review each scaffold-*.md, apply manually.\n"
        "After edits: POST /diff-summary with git diff output, read risks.\n"
        "Scout is read-only — you own all file writes.\n"
        f"If {base}/healthcheck returns non-200, proceed without it.\n"
        "```\n"
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
    base = safe_resolve(req.path) if req.path else config.REPO_ROOT
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
    result = await build_context(
        task=req.task,
        paths=req.paths,
        focus=req.focus,
        use_vector=req.use_vector,
    )

    written = mw.write_context(
        task=req.task,
        files=result["files"],
        summary=result["summary"],
        risks=result["risks"],
        suggested_files=result["suggested_files"],
        vector_hits=result["vector_hits"],
    )

    log.debug("POST /context done files=%d vector_hits=%d dur=%.2fs", len(result["files"]), len(result["vector_hits"]), time.monotonic() - t0)
    return {
        "task":            req.task,
        "files":           result["files"],
        "summary":         result["summary"],
        "risks":           result["risks"],
        "suggested_files": result["suggested_files"],
        "vector_hits":     [h.get("path", "") for h in result["vector_hits"]],
        "written_to":      written,
    }


# ── Diff Summary ───────────────────────────────────────────────────────────────

@app.post("/diff-summary")
async def diff_summary(req: DiffRequest):
    """
    Summarize a git diff: changes, risks, test recommendations.
    Writes: /output/diff-{hash}.md
    """
    t0 = time.monotonic()
    log.debug("POST /diff-summary diff_len=%d", len(req.diff))
    result  = await review_diff(req.diff)
    written = mw.write_diff(
        summary=result["summary"],
        risks=result["risks"],
        files_touched=result["files_touched"],
        test_recs=result["test_recommendations"],
    )

    log.debug("POST /diff-summary done files=%d dur=%.2fs", len(result["files_touched"]), time.monotonic() - t0)
    return {
        "summary":              result["summary"],
        "risks":                result["risks"],
        "files_touched":        result["files_touched"],
        "test_recommendations": result["test_recommendations"],
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
    Claude reviews the full batch via ai-context/scaffold-*.md before applying anything.
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
            results.append(ScaffoldFileResult(file=sf.file, mode=sf.mode, code=code, written_to=written))
            log.debug("scaffold file=%s done code_len=%d", sf.file, len(code))

        except Exception as e:
            log.error("scaffold file=%s error: %s", sf.file, e)
            errors.append(f"{sf.file}: {e}")

    log.debug("POST /scaffold done files=%d errors=%d dur=%.2fs", len(results), len(errors), time.monotonic() - t0)
    return ScaffoldResponse(task=req.task, files=results, total=len(results), errors=errors)


def _chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """Split text into overlapping chunks."""
    chunks = []
    start  = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks
