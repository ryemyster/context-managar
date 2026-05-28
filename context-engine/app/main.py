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
from fastapi import FastAPI, HTTPException
from . import config
from . import ollama_client, supabase_vector
from .logger import log
from . import markdown_writer as mw
from .models import (
    ScanRequest, FindRequest, DependenciesRequest, SummarizeRequest,
    ContextRequest, DiffRequest, VectorSearchRequest, IndexRequest,
    HealthResponse, ScanResponse, FindResponse, RoutesResponse,
    DependenciesResponse, SummarizeResponse, ContextResponse,
    DiffResponse, VectorSearchResponse, IndexResponse,
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
)


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """
    Full health status. Always returns HTTP 200 — check 'status' field.
    Use /healthcheck for monitoring (returns 503 when critical services are down).
    """
    models         = await ollama_client.list_models()
    model_ok       = any(config.OLLAMA_MODEL in m for m in models)
    embed_ok       = any(config.OLLAMA_EMBED_MODEL in m for m in models)
    supabase_ok    = await supabase_vector.is_supabase_reachable()
    vector_ok      = await supabase_vector.is_available()
    repo_mounted   = config.REPO_ROOT.exists() and config.REPO_ROOT.is_dir()

    # Degraded = can still scan/find/summarize (qwen works) but vector is down
    # Critical = Ollama or repo not reachable — nothing will work
    critical = model_ok and repo_mounted
    status = "ok" if (critical and supabase_ok) else ("degraded" if critical else "critical")

    return {
        "status":               status,
        "ollama":               len(models) > 0,
        "ollama_host":          config.OLLAMA_HOST,
        "model":                config.OLLAMA_MODEL,
        "model_available":      model_ok,
        "embed_model":          config.OLLAMA_EMBED_MODEL,
        "embed_model_available": embed_ok,
        "available_models":     models,
        "supabase":             supabase_ok,
        "vector_ready":         vector_ok,
        "supabase_url":         config.SUPABASE_URL or "not configured",
        "vector_table":         config.SUPABASE_VECTOR_TABLE,
        "repo_mounted":         repo_mounted,
        "repo_root":            str(config.REPO_ROOT),
        "output_dir":           str(config.OUTPUT_DIR),
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
            "wrong_repo":          "Update REPO_PATH in .env and restart: docker compose -f docker-compose.context.yml up -d",
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

    gen_status = "available" if model_ok else "OFFLINE — skip generation endpoints"
    emb_status = "available" if embed_ok else "OFFLINE — skip /index and /vector-search"
    vec_status = "ready — semantic search available" if vec_ok else "not indexed — /vector-search returns empty; use /find"

    return (
        "# context-engine — Agent Integration Protocol\n"
        "> You are an AI agent tool. This service is your local context scout.\n"
        "> To load this document: run `curl -s http://localhost:8088/setup` in your terminal, or ask Claude to run it via Bash.\n"
        "> Do not ask the user to explain the codebase — call the scout instead.\n"
        "\n---\n\n"

        "## What this service is\n\n"
        "context-engine is a read-only local AI context layer. It owns all mechanical repo work\n"
        "so agents spend tokens on judgment, not recall.\n\n"
        "```\n"
        "Local tokens  →  recall  (scanning, searching, summarizing, indexing)\n"
        "Agent tokens  →  judgment (planning, architecture, implementation, decisions)\n"
        "```\n\n"
        "| This service owns       | You (the agent) own   |\n"
        "|-------------------------|-----------------------|\n"
        "| repo scanning           | planning              |\n"
        "| deterministic search    | architecture          |\n"
        "| file summaries          | implementation        |\n"
        "| dependency mapping      | code edits            |\n"
        "| route extraction        | migrations            |\n"
        "| context bundles         | PR review             |\n"
        "| diff summaries          | security review       |\n"
        "| vector retrieval        | final decisions       |\n\n"
        "**Hard constraint:** this service never writes to the repo. All output goes to `./ai-context/` as Markdown.\n\n"
        f"**Path convention:** `REPO_ROOT` is `{repo}` (your `~/Repos` directory). All `path` parameters must be\n"
        "prefixed with `<owner>/<repo-name>/` — e.g. `\"ryemyster/local-model/src\"`. A bare `\".\"` scans\n"
        "**all** of `~/Repos` which is never what you want. Always scope to a specific repo.\n"
        "\n---\n\n"

        "## Live capabilities\n\n"
        "| Capability | Status |\n"
        "|---|---|\n"
        f"| Text generation (scan/find/summarize/context/diff) | {gen_status} |\n"
        f"| Embeddings + vector search | {emb_status} |\n"
        f"| Semantic search (indexed chunks) | {vec_status} |\n"
        f"| Repo being scanned | `{repo}` |\n"
        "\n---\n\n"

        "## Decision rules\n\n"
        "| Situation | Call |\n"
        "|---|---|\n"
        "| Starting any non-trivial task | `POST /context` — full bundle, always start here |\n"
        "| Need to know what is in a directory | `POST /scan` |\n"
        "| Need to find where a concept lives | `POST /find` |\n"
        "| Need all routes in a Next.js app | `POST /routes` |\n"
        "| Need to understand one specific file | `POST /summarize` |\n"
        "| Need the import graph of a path | `POST /dependencies` |\n"
        "| Have a git diff to review | `POST /diff-summary` |\n"
        "| Want semantically similar code chunks | `POST /vector-search` |\n"
        "| Engine is unreachable | Proceed without it — never block on the scout |\n\n"
        "**Skip the scout when:** the task is a one-liner, the files are already in context this session,\n"
        f"or `{base}/healthcheck` returns non-200.\n"
        "\n---\n\n"

        "## Endpoint reference\n\n"

        "### POST /context — use before every non-trivial task\n\n"
        "```json\n"
        "{\n"
        '  "task": "describe what you are about to implement",\n'
        '  "paths": ["ryemyster/local-model/src", "ascendvent/checkin/src/app"],\n'
        '  "focus": ["auth", "stripe", "relevant-terms"]\n'
        "}\n"
        "```\n\n"
        "Response fields:\n"
        "- `summary` — one-paragraph synthesis of what is relevant\n"
        "- `files` — file inventory for the scoped paths\n"
        "- `suggested_files` — files most likely relevant; verify these before editing\n"
        "- `risks` — flags from the local model\n"
        "- `vector_hits` — semantically similar chunks (empty if not indexed)\n"
        "- `written_to` — path to `./ai-context/context-bundle.md`; read this file\n\n"

        "### POST /scan\n"
        '`{"path": "ryemyster/local-model/src/app/api"}` → file list, summary, patterns · writes `scan-<slug>.md`\n\n'

        "### POST /find\n"
        '`{"query": "stripe subscription enforcement", "path": "ascendvent/checkin/src"}` → matching files + synthesis · writes `find-<slug>.md`\n\n'

        "### POST /summarize\n"
        '`{"file": "ascendvent/checkin/src/app/api/checkins/route.ts"}` → purpose, deps, risks, architectural notes · writes `summary-<slug>.md`\n\n'

        "### POST /routes\n"
        '`{"path": "ascendvent/checkin"}` → api_routes, page_routes, server_actions, middleware, auth_paths · writes `routes.md`\n\n'

        "### POST /dependencies\n"
        '`{"path": "ascendvent/checkin/src/lib"}` → internal imports, external packages, import graph · writes `dependencies-<slug>.md`\n\n'

        "### POST /diff-summary\n"
        '`{"diff": "<git diff text>"}` → summary, risks, files_touched, test_recommendations · writes `diff-<hash>.md`\n'
        "Call this after every set of edits with the output of `git diff`.\n\n"

        "### POST /vector-search\n"
        '`{"query": "auth session middleware", "limit": 8}` → ranked list of `{path, chunk, similarity}` · writes `vector-<slug>.md`\n'
        "Only useful after `/index` has been run. Check `vector_ready` in `/health` first.\n\n"

        "### POST /index\n"
        '`{"paths": ["src/app", "src/lib"], "force": false}` → indexes code chunks into pgvector using nomic-embed-text\n'
        "Run once per session when the codebase has changed. Only needed if you intend to use `/vector-search`.\n"
        "\n---\n\n"

        "## Output files\n\n"
        "All output is written to `./ai-context/` in the project directory (gitignored).\n"
        "If a recent file already covers your question, read it directly — do not re-call the endpoint.\n\n"
        "| File pattern | Endpoint | Re-use if |\n"
        "|---|---|---|\n"
        "| `context-bundle.md` | `/context` | same task this session |\n"
        "| `routes.md` | `/routes` | routes have not changed |\n"
        "| `scan-<slug>.md` | `/scan` | same path, no code changes |\n"
        "| `find-<slug>.md` | `/find` | same query |\n"
        "| `summary-<slug>.md` | `/summarize` | file has not been edited |\n"
        "| `diff-<hash>.md` | `/diff-summary` | same diff |\n"
        "| `vector-<slug>.md` | `/vector-search` | same query |\n\n"
        "**Always verify actual source files before editing** — output files are scout reports, not ground truth.\n"
        "\n---\n\n"

        "## Worked example\n\n"
        "```\n"
        "Task: Add rate limiting to the check-in API\n\n"
        "1. POST /context\n"
        '   {"task": "Add rate limiting to check-in API", "paths": ["ascendvent/checkin/src/app/api","ascendvent/checkin/src/lib"], "focus": ["rate-limit","checkins","middleware"]}\n\n'
        "2. Read ./ai-context/context-bundle.md\n"
        "   → summary: rate limiting not yet implemented\n"
        "   → suggested_files: src/app/api/checkins/route.ts, src/middleware.ts\n\n"
        "3. POST /summarize for each suggested file not yet in context\n\n"
        "4. POST /routes to confirm the middleware chain\n\n"
        "5. Plan and implement\n\n"
        "6. POST /diff-summary with git diff output → read risks + test_recommendations\n"
        "```\n"
        "\n---\n\n"

        "## Adding this integration to a project permanently\n\n"
        "Add the following to the project's `CLAUDE.md` (or equivalent agent rules file).\n"
        "This ensures every future agent session uses the scout automatically:\n\n"
        "```\n"
        "## Context Engine\n\n"
        f"A local context scout runs at {base}.\n"
        "Before any non-trivial task, call POST /context with the task description, relevant paths, and focus terms.\n"
        "Read ./ai-context/context-bundle.md before planning or editing.\n"
        "After editing, call POST /diff-summary with the git diff output and read the result.\n"
        "The scout is read-only. Always verify actual source files before making changes.\n"
        f"If {base}/healthcheck returns non-200, proceed without the scout.\n"
        "```\n"
        "\n---\n"
        f"_context-engine · repo: `{repo}` · {base} · {base}/docs_\n"
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

Matching files:
{snippet_block}

In 2-3 sentences: where is "{req.query}" implemented and what are the key files?"""
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
        prompt = f"""Analyze these Next.js route files. For each, one line:
- HTTP methods
- Auth required?
- What it does

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

    prompt = f"""Analyze this file. Respond with JSON only — no markdown fences.

File: {req.file}

```
{content[:config.MAX_TOTAL_CHARS]}
```

Respond with exactly:
{{
  "purpose": "one sentence: what this file does",
  "dependencies": ["dep1", "dep2"],
  "risks": ["risk1"],
  "architectural_notes": ["note1"]
}}"""

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

    matches  = await supabase_vector.search(embedding, limit=req.limit)
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
        return {
            "paths":     req.paths,
            "indexed":   0,
            "skipped":   0,
            "errors":    0,
            "available": False,
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


def _chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """Split text into overlapping chunks."""
    chunks = []
    start  = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks
