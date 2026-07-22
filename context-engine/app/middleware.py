"""
middleware.py — API key auth and request ID tracing middleware.
"""

import time
import traceback
import uuid
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from . import config
from .logger import log, request_id_var
from .metrics import metrics

_BYPASS_PATHS = {"/health", "/healthcheck", "/setup"}


class AuthMiddleware(BaseHTTPMiddleware):
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


class RequestLogMiddleware(BaseHTTPMiddleware):
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
        metrics.record_request(request.method, request.url.path, response.status_code, ms)
        slow = ms > config.SLOW_REQUEST_MS
        lvl = log.warning if (response.status_code >= 500 or slow) else log.info
        extra = " SLOW" if slow else ""
        lvl("%s %s %d %.0fms%s rid=%s", request.method, request.url.path, response.status_code, ms, extra, rid)
        response.headers["X-Request-Id"] = rid
        return response
