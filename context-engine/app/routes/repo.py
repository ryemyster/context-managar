"""
repo.py — static codebase analysis, search, reading, and context building routes.
"""

import time
import asyncio
from pathlib import Path
from fastapi import APIRouter, HTTPException, Body
from .. import main

class ConfigProxy:
    def __getattr__(self, name): return getattr(main.config, name)
config = ConfigProxy()

class SupabaseProxy:
    def __getattr__(self, name): return getattr(main.supabase_vector, name)
supabase_vector = SupabaseProxy()

class InferenceProxy:
    def __getattr__(self, name): return getattr(main.inference, name)
inference = InferenceProxy()

class LogProxy:
    def __getattr__(self, name): return getattr(main.log, name)
log = LogProxy()

class MwProxy:
    def __getattr__(self, name): return getattr(main.mw, name)
mw = MwProxy()

class ArtifactStoreProxy:
    def __getattr__(self, name): return getattr(main.artifact_store, name)
artifact_store = ArtifactStoreProxy()

def safe_resolve(*a, **k): return main.safe_resolve(*a, **k)
def read_file(*a, **k): return main.read_file(*a, **k)
def rel_path(*a, **k): return main.rel_path(*a, **k)
def walk_repo(*a, **k): return main.walk_repo(*a, **k)
def build_snippet_block(*a, **k): return main.build_snippet_block(*a, **k)
def find_in_repo(*a, **k): return main.find_in_repo(*a, **k)
def extract_imports(*a, **k): return main.extract_imports(*a, **k)
def extract_routes(*a, **k): return main.extract_routes(*a, **k)
def map_dependencies(*a, **k): return main.map_dependencies(*a, **k)
def scan_directory(*a, **k): return main.scan_directory(*a, **k)
def review_diff(*a, **k): return main.review_diff(*a, **k)
def build_context(*a, **k): return main.build_context(*a, **k)
from ..models import (
    ScanRequest, FindRequest, DependenciesRequest, RoutesRequest,
    ReadRequest, SummarizeRequest, ContextRequest, DiffRequest
)
from ..response_shaper import limits_for, reference, read_response, shaped_response

router = APIRouter()


@router.post("/scan")
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


@router.post("/find")
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


@router.post("/routes")
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


@router.post("/dependencies")
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


@router.post("/read")
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


@router.post("/summarize")
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


@router.post("/context")
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


@router.post("/diff-summary")
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
