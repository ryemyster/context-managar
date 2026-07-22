"""
health.py — health check, setup, debugging, and log-level routes.
"""

import logging
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
import httpx as _httpx
from fastapi import APIRouter, HTTPException, Request, Response as _Resp
from .. import main
from ..logger import TRACE
from ..metrics import HEALTH_PROBE_TTL_S

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

class MetricsProxy:
    def __getattr__(self, name): return getattr(main.metrics, name)
metrics = MetricsProxy()
from ..models import LogLevelRequest, LogLevelResponse
from ..templates.usage_guide import get_usage_guide

router = APIRouter()


def _artifact_store_writable() -> bool:
    try:
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        probe = config.OUTPUT_DIR / ".healthcheck-write"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _records_dir() -> Path:
    return config.OUTPUT_DIR / "records"


def _rewrite_local_mcp_host(host: str) -> str:
    parsed = urlsplit(f"http://{host}")
    hostname = parsed.hostname or ""
    port = parsed.port
    if hostname in {"127.0.0.1", "localhost"}:
        target_port = 8089 if port in {None, 8088} else port
        return f"{hostname}:{target_port}"
    return host


def _public_urls(request: Request | None) -> tuple[str, str]:
    if config.CONTEXT_ENGINE_PUBLIC_BASE_URL:
        base = config.CONTEXT_ENGINE_PUBLIC_BASE_URL.rstrip("/")
    elif request is not None:
        forwarded_proto = request.headers.get("x-forwarded-proto")
        forwarded_host = request.headers.get("x-forwarded-host")
        if forwarded_proto and forwarded_host:
            base = f"{forwarded_proto}://{forwarded_host}".rstrip("/")
        else:
            base = str(request.base_url).rstrip("/")
    else:
        base = "http://localhost:8088"

    if config.CONTEXT_ENGINE_PUBLIC_MCP_URL:
        mcp_url = config.CONTEXT_ENGINE_PUBLIC_MCP_URL.rstrip("/")
    elif request is not None:
        forwarded_proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
        if "x-forwarded-host" not in request.headers:
            forwarded_host = _rewrite_local_mcp_host(forwarded_host)
        mcp_url = f"{forwarded_proto}://{forwarded_host}/mcp".rstrip("/")
    else:
        mcp_url = "http://127.0.0.1:8089/mcp"
    return base, mcp_url


def _artifact_summary() -> dict:
    records_dir = _records_dir()
    markdown_dir = config.OUTPUT_DIR / "markdown"
    summary = {
        "records": 0,
        "markdown": 0,
        "events_log_bytes": 0,
        "records_bytes": 0,
        "markdown_bytes": 0,
    }
    if records_dir.exists():
        for path in records_dir.glob("*.json"):
            summary["records"] += 1
            try:
                summary["records_bytes"] += path.stat().st_size
            except Exception:
                pass
    if markdown_dir.exists():
        for path in markdown_dir.glob("*.md"):
            summary["markdown"] += 1
            try:
                summary["markdown_bytes"] += path.stat().st_size
            except Exception:
                pass
    event_log = config.OUTPUT_DIR / "events.jsonl"
    if event_log.exists():
        try:
            summary["events_log_bytes"] = event_log.stat().st_size
        except Exception:
            pass
    return summary


def _context_note_summary() -> dict:
    total = 0
    by_repo: dict[str, int] = {}
    by_scope: dict[str, int] = {}
    by_source: dict[str, int] = {}
    warnings = 0
    records_dir = _records_dir()
    if not records_dir.exists():
        return {
            "total": total,
            "by_repo": by_repo,
            "by_scope": by_scope,
            "by_source": by_source,
            "warning_count": warnings,
        }
    for path in records_dir.glob("context_note-*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            request = record.get("request") or {}
            response = record.get("response") or {}
            repo = str(request.get("repo") or "unknown")
            scope = str(request.get("scope") or "unknown")
            source = str(request.get("source") or "unknown")
            total += 1
            by_repo[repo] = by_repo.get(repo, 0) + 1
            by_scope[scope] = by_scope.get(scope, 0) + 1
            by_source[source] = by_source.get(source, 0) + 1
            warnings += len(response.get("warnings") or [])
        except Exception:
            continue
    return {
        "total": total,
        "by_repo": by_repo,
        "by_scope": by_scope,
        "by_source": by_source,
        "warning_count": warnings,
    }


async def _embedding_probe(force: bool = False) -> dict:
    cached = None if force else metrics.get_health_probe("embedding_round_trip")
    if cached:
        return cached
    started = datetime.now(tz=timezone.utc)
    t0 = time.monotonic()
    try:
        vector = await inference.embed("context-engine health probe")
        duration_ms = (time.monotonic() - t0) * 1000
        result = {
            "ok": bool(vector),
            "status": "ok" if vector else "degraded",
            "latency_ms": round(duration_ms, 2),
            "vector_dimensions": len(vector) if vector else 0,
            "started_at": started.isoformat(),
            "probe_ttl_s": HEALTH_PROBE_TTL_S,
        }
    except Exception as exc:
        result = {
            "ok": False,
            "status": "critical",
            "latency_ms": round((time.monotonic() - t0) * 1000, 2),
            "error": str(exc),
            "started_at": started.isoformat(),
            "probe_ttl_s": HEALTH_PROBE_TTL_S,
        }
    metrics.update_health_probe("embedding_round_trip", result)
    return result


async def _vector_probe(force: bool = False) -> dict:
    cached = None if force else metrics.get_health_probe("vector_round_trip")
    if cached:
        return cached
    started = datetime.now(tz=timezone.utc)
    t0 = time.monotonic()
    try:
        available = await supabase_vector.is_available()
        if not available:
            result = {
                "ok": False,
                "status": "critical",
                "latency_ms": 0.0,
                "results": 0,
                "error": "vector store unavailable",
                "started_at": started.isoformat(),
                "probe_ttl_s": HEALTH_PROBE_TTL_S,
            }
        else:
            embedding = await inference.embed("context-engine vector probe")
            if not embedding:
                result = {
                    "ok": False,
                    "status": "critical",
                    "latency_ms": 0.0,
                    "results": 0,
                    "error": "embedding probe failed",
                    "started_at": started.isoformat(),
                    "probe_ttl_s": HEALTH_PROBE_TTL_S,
                }
            else:
                results = await supabase_vector.search(embedding, limit=1, threshold=0.0)
                result = {
                    "ok": True,
                    "status": "ok",
                    "latency_ms": round((time.monotonic() - t0) * 1000, 2),
                    "results": len(results),
                    "started_at": started.isoformat(),
                    "probe_ttl_s": HEALTH_PROBE_TTL_S,
                }
    except Exception as exc:
        result = {
            "ok": False,
            "status": "critical",
            "latency_ms": round((time.monotonic() - t0) * 1000, 2),
            "results": 0,
            "error": str(exc),
            "started_at": started.isoformat(),
            "probe_ttl_s": HEALTH_PROBE_TTL_S,
        }
    metrics.update_health_probe("vector_round_trip", result)
    return result


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
    vector_rows    = await supabase_vector.vector_row_count()
    repo_mounted   = config.REPO_ROOT.exists() and config.REPO_ROOT.is_dir()
    artifact_ok    = _artifact_store_writable()
    embedding_probe = await _embedding_probe()
    vector_probe = await _vector_probe()

    # Degraded = can still scan/find/summarize but vector or reasoning is down
    # Critical = code model or repo not reachable — nothing will work
    critical = model_ok and repo_mounted and artifact_ok
    status = "ok" if (critical and reason_ok and supabase_ok and vector_probe["ok"] and embedding_probe["ok"]) else ("degraded" if critical else "critical")

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
        "vector_row_count":       vector_rows,
        "supabase_url":           config.SUPABASE_URL or "not configured",
        "vector_table":           config.SUPABASE_VECTOR_TABLE,
        "repo_mounted":           repo_mounted,
        "repo_root":              str(config.REPO_ROOT),
        "output_dir":             str(config.OUTPUT_DIR),
        "artifact_store_writable": artifact_ok,
        "checks": {
            "ollama_generation": {
                "ok": model_ok,
                "model": inference.settings.fast_model,
                "available": model_ok,
            },
            "ollama_reasoning": {
                "ok": reason_ok,
                "model": inference.settings.reasoning_model,
                "available": reason_ok,
            },
            "ollama_embedding": {
                "ok": embed_ok and embedding_probe["ok"],
                "model": inference.settings.embedding_model,
                "available": embed_ok,
                "round_trip": embedding_probe,
            },
            "supabase": {
                "ok": supabase_ok,
                "reachable": supabase_ok,
            },
            "vector_store": {
                "ok": vector_ok and vector_probe["ok"],
                "ready": vector_ok,
                "row_count": vector_rows,
                "round_trip": vector_probe,
            },
            "artifact_store": {
                "ok": artifact_ok,
                "writable": artifact_ok,
                "summary": _artifact_summary(),
            },
            "repo_root": {
                "ok": repo_mounted,
                "readable": repo_mounted,
                "path": str(config.REPO_ROOT),
            },
        },
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
async def setup(request: Request):
    """
    Operational playbook for AI agents.

    This endpoint is intentionally context-safe: it teaches retrieval behavior
    without embedding a full configuration manual into the caller's session.
    """
    repo       = str(config.REPO_ROOT)
    base, mcp_url = _public_urls(request)

    return get_usage_guide(
        base=base,
        repo=repo,
        mcp_url=mcp_url,
        ollama_timeout=int(config.OLLAMA_TIMEOUT),
        reason_timeout=int(config.OLLAMA_REASON_TIMEOUT),
        agent_timeout=int(config.OLLAMA_AGENT_TIMEOUT),
        call_timeout=int(config.OLLAMA_AGENT_CALL_TIMEOUT),
    )


@router.get("/stats")
async def stats():
    snapshot = metrics.stats_snapshot()
    snapshot["artifacts"] = _artifact_summary()
    snapshot["context_notes"] = _context_note_summary()
    snapshot["health_probes"] = {
        key: probe
        for key in ("embedding_round_trip", "vector_round_trip")
        if (probe := metrics.get_health_probe(key))
    }
    return snapshot


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
