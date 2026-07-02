"""
health.py — health check, setup, debugging, and log-level routes.
"""

import logging
from datetime import datetime, timezone
import httpx as _httpx
from fastapi import APIRouter, HTTPException, Response as _Resp
from .. import main
from ..logger import TRACE

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
from ..models import LogLevelRequest, LogLevelResponse
from ..templates.usage_guide import get_usage_guide

router = APIRouter()


@router.get("/health")
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


@router.get("/healthcheck")
async def healthcheck():
    """
    Monitoring-friendly endpoint. Returns HTTP 200 or 503.
    200 = Ollama reachable + models available + repo mounted (Supabase optional)
    503 = critical services down (Ollama or repo missing)

    Used by Docker health check and external monitors.
    curl http://localhost:8088/healthcheck → {"ok": true} or {"ok": false, "reason": "..."}
    """
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


@router.get("/debug")
async def debug():
    """
    Full diagnostic: model state, vector row count, config, output files.
    Use this to troubleshoot. No model calls made.
    """
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


@router.get("/setup", response_class=_Resp)
async def setup():
    """
    Operational playbook for AI agents.

    This endpoint is intentionally context-safe: it teaches retrieval behavior
    without embedding a full configuration manual into the caller's session.
    """
    repo       = str(config.REPO_ROOT)
    base       = "http://localhost:8088"
    mcp_url    = "http://127.0.0.1:8089/mcp"

    return get_usage_guide(
        base=base,
        repo=repo,
        mcp_url=mcp_url,
    )


@router.get("/log-level", response_model=LogLevelResponse)
async def get_log_level():
    current = logging.getLevelName(log.level)
    return LogLevelResponse(previous=current, current=current)


@router.post("/log-level", response_model=LogLevelResponse)
async def set_log_level(req: LogLevelRequest):
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
