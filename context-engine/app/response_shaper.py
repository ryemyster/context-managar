"""
Context-safe response shaping for discovery endpoints.

Discovery endpoints should return references by default. Full content remains
available through detail="full" or the dedicated /read endpoint.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


SUMMARY_MAX_CHARS = 4200
STANDARD_MAX_CHARS = 12000
CONTEXT_SAFE_MAX_CHARS = 2400
SUMMARY_MAX_RESULTS = 20
CONTEXT_SAFE_MAX_RESULTS = 8


def _stable_id(kind: str, path: str, suffix: str = "") -> str:
    raw = f"{kind}:{path}:{suffix}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _clip(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    if max_chars <= 20:
        return text[:max_chars], True
    return text[: max_chars - 14].rstrip() + " [truncated]", True


def _payload_bytes(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8"))


def _estimated_tokens(payload: dict[str, Any]) -> int:
    return max(1, _payload_bytes(payload) // 4)


def limits_for(req: Any) -> tuple[str, int, int]:
    detail = getattr(req, "detail", "summary") or "summary"
    mode = getattr(req, "mode", None)
    if mode == "context_safe":
        detail = "summary"
        default_results = CONTEXT_SAFE_MAX_RESULTS
        default_chars = CONTEXT_SAFE_MAX_CHARS
    elif detail == "standard":
        default_results = SUMMARY_MAX_RESULTS
        default_chars = STANDARD_MAX_CHARS
    elif detail == "full":
        default_results = getattr(req, "limit", None) or getattr(req, "max_results", None) or 1000
        default_chars = getattr(req, "max_chars", None) or 1_000_000
    else:
        default_results = SUMMARY_MAX_RESULTS
        default_chars = SUMMARY_MAX_CHARS

    max_results = getattr(req, "max_results", None) or getattr(req, "limit", None) or default_results
    max_chars = getattr(req, "max_chars", None) or default_chars
    return detail, max(1, int(max_results)), max(200, int(max_chars))


def reference(
    *,
    kind: str,
    path: str,
    title: str | None = None,
    summary: str = "",
    score: float | None = None,
    suffix: str = "",
    max_summary_chars: int = 280,
) -> dict[str, Any]:
    clipped, _ = _clip(" ".join(str(summary).split()), max_summary_chars)
    return {
        "id": _stable_id(kind, path, suffix),
        "title": title or path,
        "type": kind,
        "score": score,
        "path": path,
        "summary": clipped,
    }


def shaped_response(
    *,
    detail: str,
    results: list[dict[str, Any]],
    full_payload: dict[str, Any],
    max_results: int,
    max_chars: int,
    written_to: str = "",
    available: bool | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    truncated = len(results) > max_results
    limited_results = results[:max_results]

    if detail == "full":
        payload = dict(full_payload)
        payload["metadata"] = {
            "result_count": len(results),
            "payload_bytes": 0,
            "estimated_tokens": 0,
            "truncated": False,
            "detail_level": "full",
        }
        payload["deprecation_warnings"] = [
            "Large inline discovery payloads are deprecated; use detail='summary' plus /read for file content."
        ]
        payload["metadata"]["payload_bytes"] = _payload_bytes(payload)
        payload["metadata"]["estimated_tokens"] = _estimated_tokens(payload)
        return payload

    payload: dict[str, Any] = {
        "results": limited_results,
        "written_to": written_to,
        "metadata": {
            "result_count": len(results),
            "payload_bytes": 0,
            "estimated_tokens": 0,
            "truncated": truncated,
            "detail_level": detail,
        },
    }
    if available is not None:
        payload["available"] = available
    if warnings:
        payload["warnings"] = warnings

    while _payload_bytes(payload) > max_chars and payload["results"]:
        payload["results"] = payload["results"][:-1]
        payload["metadata"]["truncated"] = True
    payload["metadata"]["payload_bytes"] = _payload_bytes(payload)
    payload["metadata"]["estimated_tokens"] = _estimated_tokens(payload)
    return payload


def read_response(path: str, content: str, max_chars: int) -> dict[str, Any]:
    clipped, truncated = _clip(content, max_chars)
    payload = {
        "id": _stable_id("file", path),
        "title": path.rsplit("/", 1)[-1],
        "type": "file",
        "path": path,
        "content": clipped,
        "metadata": {
            "result_count": 1,
            "payload_bytes": 0,
            "estimated_tokens": 0,
            "truncated": truncated,
            "detail_level": "full",
        },
    }
    payload["metadata"]["payload_bytes"] = _payload_bytes(payload)
    payload["metadata"]["estimated_tokens"] = _estimated_tokens(payload)
    return payload
