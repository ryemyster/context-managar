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

from contextlib import asynccontextmanager
from fastapi import FastAPI
from . import config
from .inference import inference
from .logger import log
from .middleware import AuthMiddleware, RequestLogMiddleware

# Expose dependencies mocked in tests and needed by routers first
# (This avoids circular imports when routes are loaded)
from . import supabase_vector
from .inference import inference
from .repo_reader import safe_resolve, read_file, rel_path, walk_repo, build_snippet_block
from .route_extractor import extract_routes
from .search_worker import find_in_repo, extract_imports
from .dependency_mapper import map_dependencies
from .scanner import scan_directory
from .diff_reviewer import review_diff
from .context_builder import build_context, _issue_numbers
from . import artifact_store
from . import markdown_writer as mw
from .utils import chunk_text
from . import tool_registry
from . import agent_runner
from .issue_auditor import collect_agent_evidence, findings_from_evidence, insufficient_evidence_findings
from .metrics import metrics


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


app = FastAPI(
    title="context-engine",
    version="1.0.0",
    description="Local AI context layer for Claude Code. Scout, not engineer.",
    lifespan=lifespan,
)

# Apply middlewares
app.add_middleware(RequestLogMiddleware)
app.add_middleware(AuthMiddleware)


# NOW import and register modular router endpoints at the end to prevent circular dependency
from .routes.health import router as health_router, setup, health, healthcheck, debug, stats, get_log_level, set_log_level
from .routes.repo import router as repo_router, scan, find, routes, dependencies, read, summarize, context, store_context_note, diff_summary
from .routes.vector import router as vector_router, vector_search, index
from .routes.agents import router as agents_router, draft, scaffold, tools_call, agents_tools, agents_run, agents_run_status, issue_auditor, issue_auditor_status

app.include_router(health_router)
app.include_router(repo_router)
app.include_router(vector_router)
app.include_router(agents_router)
