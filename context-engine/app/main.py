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
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from . import config
from . import supabase_vector
from .inference import inference
from .logger import log, request_id_var
from .utils import chunk_text


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("context-engine starting")
    log.info("  repo_root=%s", config.REPO_ROOT)
    log.info(
        "  generation_provider=%s endpoint=%s model=%s",
        inference.settings.generation.name,
        inference.settings.generation.endpoint,
        inference.settings.fast_model,
    )
    log.info("  reason_model=%s", inference.settings.reasoning_model)
    log.info("  agent_model=%s", inference.settings.agent_model)
    log.info(
        "  embedding_provider=%s endpoint=%s model=%s",
        inference.settings.embedding.name,
        inference.settings.embedding.endpoint,
        inference.settings.embedding_model,
    )
    log.info("  supabase=%s", config.SUPABASE_URL or "not configured")
    log.info("  log_level=%s", config.LOG_LEVEL)
    log.info("context-engine ready on :8088")
    yield
    log.info("context-engine shutting down")
from . import markdown_writer as mw
from . import artifact_store
from .models import (
    ScanRequest, FindRequest, DependenciesRequest, RoutesRequest, ReadRequest, SummarizeRequest,
    ContextRequest, DiffRequest, VectorSearchRequest, IndexRequest, DraftRequest, ScaffoldRequest, IssueAuditRequest,
    AgentRunRequest, AgentRunResponse,
    LogLevelRequest, LogLevelResponse,
    ToolCallRequest,
    DraftResponse, ScaffoldResponse, ScaffoldFileResult, IssueAuditResponse,
)
from . import agent_runner, tool_registry
from .repo_reader import safe_resolve, read_file, rel_path, walk_repo, build_snippet_block
from .search_worker import find_in_repo, extract_imports
from .route_extractor import extract_routes
from .dependency_mapper import map_dependencies
from .scanner import scan_directory
from .response_shaper import limits_for, reference, read_response, shaped_response
from .diff_reviewer import review_diff
from .context_builder import build_context
from .issue_auditor import collect_agent_evidence, findings_from_evidence, insufficient_evidence_findings
from .context_builder import _issue_numbers

from .middleware import AuthMiddleware, RequestLogMiddleware
from .templates.usage_guide import get_usage_guide

app = FastAPI(
    title="context-engine",
    version="1.0.0",
    description="Local AI context layer for Claude Code. Scout, not engineer.",
    lifespan=lifespan,
)

app.add_middleware(RequestLogMiddleware)
app.add_middleware(AuthMiddleware)


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """
    Full health status. Always returns HTTP 200 — check 'status' field.
    Use /healthcheck for monitoring (returns 503 when critical services are down).
    """
    models         = await inference.list_models()
    embed_models   = await inference.list_embedding_models()
    model_ok       = any(inference.settings.fast_model in m for m in models)
    reason_ok      = any(inference.settings.reasoning_model in m for m in models)
    arch_ok        = any(inference.settings.agent_model in m for m in models)
    agent_ok       = any(inference.settings.agent_model in m for m in models)
    embed_ok       = any(inference.settings.embedding_model in m for m in embed_models)
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
        "ollama_host":            inference.settings.generation.endpoint,
        "code_model":             inference.settings.fast_model,
        "code_model_available":   model_ok,
        "reason_model":           inference.settings.reasoning_model,
        "reason_model_available": reason_ok,
        "arch_model":             inference.settings.agent_model,
        "arch_model_available":   arch_ok,
        "agent_model":            inference.settings.agent_model,
        "agent_model_available":  agent_ok,
        "embed_model":            inference.settings.embedding_model,
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
    models       = await inference.list_models()
    model_ok     = any(inference.settings.fast_model in m for m in models)
    repo_mounted = config.REPO_ROOT.exists() and config.REPO_ROOT.is_dir()

    if not model_ok:
        return _Resp(
            content=f'{{"ok":false,"reason":"model {inference.settings.fast_model!r} not available in inference provider"}}',
            status_code=503,
            media_type="application/json",
        )
    if not repo_mounted:
        return _Resp(
            content=f'{{"ok":false,"reason":"repo not mounted at {config.REPO_ROOT}"}}',
            status_code=503,
            media_type="application/json",
        )
    return {"ok": True, "model": inference.settings.fast_model, "repo": str(config.REPO_ROOT)}


# ── Debug ──────────────────────────────────────────────────────────────────────

@app.get("/debug")
async def debug():
    """
    Full diagnostic: model state, vector row count, config, output files.
    Use this to troubleshoot. No model calls made.
    """
    import httpx as _httpx
    from datetime import datetime, timezone

    # Preserve the legacy field while querying through the inference boundary.
    loaded_model = await inference.loaded_embedding_models() or None

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
        "generation_provider":  inference.settings.generation.name,
        "generation_endpoint":  inference.settings.generation.endpoint,
        "embedding_provider":   inference.settings.embedding.name,
        "embedding_endpoint":   inference.settings.embedding.endpoint,
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
    Operational playbook for AI agents.

    This endpoint is intentionally context-safe: it teaches retrieval behavior
    without embedding a full configuration manual into the caller's session.
    """
    models       = await inference.list_models()
    embed_models = await inference.list_embedding_models()
    model_ok   = any(inference.settings.fast_model in m for m in models)
    arch_ok    = any(inference.settings.agent_model in m for m in models)
    reason_ok  = any(inference.settings.reasoning_model in m for m in models)
    embed_ok   = any(inference.settings.embedding_model in m for m in embed_models)
    vec_ok     = await supabase_vector.is_available()
    repo       = str(config.REPO_ROOT)
    base       = "http://localhost:8088"
    mcp_url    = "http://127.0.0.1:8089/mcp"

    gen_status    = "available" if model_ok  else "offline"
    arch_status   = "available" if arch_ok   else "offline"
    reason_status = "available" if reason_ok else "offline"
    emb_status    = "available" if embed_ok  else "offline"
    vec_status    = "ready"     if vec_ok    else "not indexed"

    return get_usage_guide(
        base=base,
        repo=repo,
        mcp_url=mcp_url,
        gen_status=gen_status,
        arch_status=arch_status,
        reason_status=reason_status,
        emb_status=emb_status,
        vec_status=vec_status,
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
    detail, max_results, max_chars = limits_for(req)

    written = mw.write_scan(
        path=req.path,
        files=result["files"],
        summary=result["summary"],
        patterns=result["patterns"],
        dependencies=result["dependencies"],
    )

    asyncio.create_task(supabase_vector.store_artifact(written))
    log.debug("POST /scan done files=%d dur=%.2fs", len(result["files"]), time.monotonic() - t0)
    full_payload = {
        "path":         req.path,
        "files":        result["files"],
        "summary":      result["summary"],
        "patterns":     result["patterns"],
        "dependencies": result["dependencies"],
        "written_to":   written,
    }
    refs = [
        reference(
            kind="file",
            path=path,
            title=path.rsplit("/", 1)[-1],
            summary=f"Discovered by scan of {req.path}. Read with /read for content.",
            score=1.0,
            suffix=str(i),
        )
        for i, path in enumerate(result["files"])
    ]
    if detail == "standard":
        refs.insert(0, reference(
            kind="scan",
            path=req.path,
            title=f"Scan summary: {req.path}",
            summary=f"{result['summary']} Patterns: {', '.join(result['patterns'][:5])}. Dependencies: {', '.join(result['dependencies'][:10])}.",
            score=1.0,
        ))
    return shaped_response(
        detail=detail,
        results=refs,
        full_payload=full_payload,
        max_results=max_results,
        max_chars=max_chars,
        written_to=written,
    )


# ── Find ───────────────────────────────────────────────────────────────────────

@app.post("/find")
async def find(req: FindRequest):
    """
    Grep the repo for query terms. One model synthesis call on top matches.
    Writes: /output/find-{slug}.md
    """
    t0 = time.monotonic()
    log.debug("POST /find query=%r path=%s", req.query, req.path or "/")
    detail, max_results, max_chars = limits_for(req)
    matches = find_in_repo(req.query, req.path, max_results=max_results + 1)

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
        synthesis = await inference.generate(prompt)
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
    full_payload = {
        "query":      req.query,
        "path":       req.path,
        "matches":    [m["path"] for m in matches],
        "files":      [m["path"] for m in matches],
        "written_to": written,
    }
    refs = [
        reference(
            kind="match",
            path=m["path"],
            title=f"{m['path']}:{m.get('line_no', 1)}",
            summary=f"Line {m.get('line_no', 1)}: {m.get('line', '').strip()}",
            score=1.0 - min(i, 20) * 0.02,
            suffix=str(m.get("line_no", i)),
        )
        for i, m in enumerate(matches)
    ]
    return shaped_response(
        detail=detail,
        results=refs,
        full_payload=full_payload,
        max_results=max_results,
        max_chars=max_chars,
        written_to=written,
    )


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.post("/routes")
async def routes(req: RoutesRequest = Body(default_factory=RoutesRequest)):
    """
    Extract all Next.js routes. Deterministic + one model analysis call.
    Writes: /output/routes.md
    """
    t0 = time.monotonic()
    log.debug("POST /routes")
    detail, max_results, max_chars = limits_for(req)
    base = safe_resolve(req.path) if req.path and req.path != "." else None
    routes_data = extract_routes(base)

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
        analysis = await inference.generate(prompt)
    else:
        analysis = "No API route files detected."

    written = mw.write_routes(routes_data, analysis)

    all_routes = list(dict.fromkeys(
        [r["path"] for r in routes_data["api_routes"]] +
        routes_data["page_routes"]
    ))

    asyncio.create_task(supabase_vector.store_artifact(written))
    log.debug("POST /routes done api=%d pages=%d dur=%.2fs", len(routes_data["api_routes"]), len(routes_data["page_routes"]), time.monotonic() - t0)
    full_payload = {
        "routes":         all_routes,
        "api_routes":     [r["path"] for r in routes_data["api_routes"]],
        "server_actions": routes_data["server_actions"],
        "middleware":     [m["path"] for m in routes_data["middleware"]],
        "auth_paths":     routes_data["auth_paths"],
        "written_to":     written,
    }
    refs: list[dict] = []
    for r in routes_data["api_routes"]:
        refs.append(reference(
            kind="api_route",
            path=r["path"],
            title=r["path"],
            summary=f"API route methods: {', '.join(r.get('methods') or ['unknown'])}. Read with /read for handler content.",
            score=1.0,
        ))
    refs.extend(reference(
        kind="page_route",
        path=path,
        title=path,
        summary="Next.js page route. Read with /read for content.",
        score=0.9,
    ) for path in routes_data["page_routes"])
    refs.extend(reference(
        kind="middleware",
        path=m["path"],
        title=m["path"],
        summary=f"Middleware matchers: {', '.join(m.get('matchers') or ['unspecified'])}.",
        score=0.95,
    ) for m in routes_data["middleware"])
    return shaped_response(
        detail=detail,
        results=refs,
        full_payload=full_payload,
        max_results=max_results,
        max_chars=max_chars,
        written_to=written,
    )


# ── Dependencies ───────────────────────────────────────────────────────────────

@app.post("/dependencies")
async def dependencies(req: DependenciesRequest):
    """
    Map all imports and dependencies. Purely deterministic — no model call.
    Writes: /output/dependencies-{slug}.md
    """
    t0 = time.monotonic()
    log.debug("POST /dependencies path=%s", req.path or "/")
    detail, max_results, max_chars = limits_for(req)
    dep_data = map_dependencies(req.path)
    written  = mw.write_dependencies(req.path, dep_data)
    asyncio.create_task(supabase_vector.store_artifact(written))
    log.debug("POST /dependencies done dur=%.2fs", time.monotonic() - t0)

    full_payload = {
        "path":       req.path,
        "internal":   dep_data["internal"],
        "external":   dep_data["external"],
        "graph":      dep_data["graph"],
        "written_to": written,
    }
    refs = [
        reference(
            kind="dependency_file",
            path=path,
            title=path,
            summary=f"Imports: {', '.join(imports[:8]) or 'none detected'}.",
            score=1.0 - min(i, 20) * 0.02,
        )
        for i, (path, imports) in enumerate(dep_data["graph"].items())
    ]
    if detail == "standard":
        refs.insert(0, reference(
            kind="dependency_summary",
            path=req.path,
            title=f"Dependencies: {req.path}",
            summary=f"External packages: {', '.join(dep_data['external'][:20])}. Internal imports: {', '.join(dep_data['internal'][:20])}.",
            score=1.0,
        ))
    return shaped_response(
        detail=detail,
        results=refs,
        full_payload=full_payload,
        max_results=max_results,
        max_chars=max_chars,
        written_to=written,
    )


# ── Read ───────────────────────────────────────────────────────────────────────

@app.post("/read")
async def read(req: ReadRequest):
    """
    Dedicated content fetch endpoint.

    Search/discovery endpoints return references by default; callers use /read
    when they intentionally want file content in the response.
    """
    f = safe_resolve(req.path)
    if not f.exists() or not f.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {req.path!r}")
    max_chars = req.max_chars or config.MAX_FILE_BYTES
    content = read_file(f, max_bytes=max_chars + 1)
    if not content:
        raise HTTPException(status_code=422, detail="File is empty or unreadable")
    return read_response(req.path, content, max_chars)


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

    raw    = await inference.generate(prompt)
    parsed = inference.parse_json_response(raw)

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
    detail, max_results, max_chars = limits_for(req)
    available = await supabase_vector.is_available()

    if not available:
        log.debug("POST /vector-search skipped — vector store not available")
        full_payload = {
            "query":      req.query,
            "matches":    [],
            "written_to": "",
            "available":  False,
        }
        return shaped_response(
            detail=detail,
            results=[],
            full_payload=full_payload,
            max_results=max_results,
            max_chars=max_chars,
            available=False,
        )

    embedding = await inference.embed(req.query)
    if embedding is None:
        log.warning("POST /vector-search embed failed query=%r", req.query)
        full_payload = {
            "query":      req.query,
            "matches":    [],
            "written_to": "",
            "available":  False,
        }
        return shaped_response(
            detail=detail,
            results=[],
            full_payload=full_payload,
            max_results=max_results,
            max_chars=max_chars,
            available=False,
        )

    matches  = await supabase_vector.search(embedding, limit=max_results + 1, threshold=req.threshold)
    written  = mw.write_vector_results(req.query, matches)

    log.debug("POST /vector-search done matches=%d dur=%.2fs", len(matches), time.monotonic() - t0)
    full_payload = {
        "query":      req.query,
        "matches":    matches,
        "written_to": written,
        "available":  True,
    }
    refs = [
        reference(
            kind="vector_match",
            path=m.get("path", ""),
            title=m.get("path", ""),
            summary=m.get("chunk", ""),
            score=m.get("similarity"),
            suffix=str(i),
        )
        for i, m in enumerate(matches)
    ]
    return shaped_response(
        detail=detail,
        results=refs,
        full_payload=full_payload,
        max_results=max_results,
        max_chars=max_chars,
        written_to=written,
        available=True,
    )


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
                chunks = chunk_text(content, chunk_size=500, overlap=50)

                for chunk in chunks:
                    if not chunk.strip():
                        continue

                    h = supabase_vector.chunk_hash(rp, chunk)

                    if not req.force and await supabase_vector.already_indexed(h):
                        skipped += 1
                        continue

                    embedding = await inference.embed(f"{rp}\n{chunk}")
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

    code = await inference.generate(prompt)

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

            code = await inference.generate(prompt)

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



