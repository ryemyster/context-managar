"""
supabase_vector.py — Supabase pgvector integration.

Uses the Supabase REST API (PostgREST) via httpx.
No supabase-py dependency — keeps the image lean.

ALL operations degrade gracefully:
- If Supabase is not configured → return empty/None
- If table doesn't exist → return empty/None
- If network is unreachable → return empty/None

The calling code must never crash because vector ops failed.
"""

import hashlib
from typing import Any
import httpx
from . import config
from .logger import log

_client: httpx.AsyncClient | None = None
_vector_ready: bool | None = None   # cached after first check


def _headers() -> dict[str, str]:
    return {
        "apikey":        config.SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }


def get_client() -> httpx.AsyncClient | None:
    global _client
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        return None
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=config.SUPABASE_URL,
            timeout=15.0,
        )
    return _client


async def is_available() -> bool:
    """
    Check if Supabase is reachable and the vector table exists.
    Result is cached after first successful check.
    """
    global _vector_ready
    if _vector_ready is not None:
        return _vector_ready

    client = get_client()
    if client is None:
        _vector_ready = False
        return False

    try:
        # Query table with limit 0 just to check it exists
        r = await client.get(
            f"/rest/v1/{config.SUPABASE_VECTOR_TABLE}",
            params={"limit": "0"},
            headers=_headers(),
        )
        _vector_ready = r.status_code in (200, 206)
        log.debug("supabase vector check status=%d ready=%s", r.status_code, _vector_ready)
        return _vector_ready
    except Exception as e:
        log.warning("supabase vector check failed: %s", e)
        _vector_ready = False
        return False


async def is_supabase_reachable() -> bool:
    """Lightweight check — just hits the health endpoint."""
    client = get_client()
    if client is None:
        return False
    try:
        r = await client.get("/rest/v1/", headers=_headers(), timeout=3.0)
        return r.status_code < 500
    except Exception:
        return False


def chunk_hash(path: str, chunk: str) -> str:
    return hashlib.sha256(f"{path}:{chunk}".encode()).hexdigest()[:32]


async def already_indexed(hash_val: str) -> bool:
    """Check if a chunk with this hash already exists in the vector table."""
    client = get_client()
    if client is None:
        return False
    try:
        r = await client.get(
            f"/rest/v1/{config.SUPABASE_VECTOR_TABLE}",
            params={"chunk_hash": f"eq.{hash_val}", "select": "id", "limit": "1"},
            headers=_headers(),
        )
        if r.status_code == 200:
            return len(r.json()) > 0
    except Exception:
        pass
    return False


async def upsert_chunk(
    path: str,
    chunk: str,
    embedding: list[float],
) -> bool:
    """
    Store a code chunk + embedding in Supabase.
    Returns True on success, False on any failure.
    """
    client = get_client()
    if client is None:
        return False

    hash_val = chunk_hash(path, chunk)

    try:
        r = await client.post(
            f"/rest/v1/{config.SUPABASE_VECTOR_TABLE}",
            json={
                "path":       path,
                "chunk":      chunk,
                "chunk_hash": hash_val,
                "embedding":  embedding,
            },
            headers={
                **_headers(),
                "Prefer": "resolution=merge-duplicates",
            },
        )
        return r.status_code in (200, 201)
    except Exception:
        return False


async def search(
    query_embedding: list[float],
    limit: int = 8,
    threshold: float = 0.3,
) -> list[dict[str, Any]]:
    """
    Run vector similarity search via Supabase RPC.
    Returns list of {path, chunk, similarity} dicts.
    Returns empty list on any failure.
    """
    client = get_client()
    if client is None:
        return []

    log.debug("supabase vector search limit=%d threshold=%.2f", limit, threshold)
    try:
        r = await client.post(
            f"/rest/v1/rpc/{config.SUPABASE_MATCH_FUNCTION}",
            json={
                "query_embedding": query_embedding,
                "match_count":     limit,
                "match_threshold": threshold,
            },
            headers={**_headers(), "Prefer": ""},
        )
        if r.status_code == 200:
            results = r.json() or []
            log.debug("supabase vector search done hits=%d", len(results))
            return results
    except Exception as e:
        log.warning("supabase vector search failed: %s", e)
    return []


async def store_artifact(file_path: str) -> None:
    """
    Index a context-engine output artifact into the vector store.
    Fire-and-forget — called after every endpoint write. Never raises.
    """
    if not await is_available():
        return
    try:
        from pathlib import Path
        from . import ollama_client

        path = Path(file_path)
        if not path.exists():
            return
        content = path.read_text(encoding="utf-8")
        if not content.strip():
            return

        chunks = _chunk_text(content)
        for chunk in chunks:
            if not chunk.strip():
                continue
            h = chunk_hash(file_path, chunk)
            if await already_indexed(h):
                continue
            embedding = await ollama_client.embed(f"{path.name}\n{chunk}")
            if embedding:
                await upsert_chunk(file_path, chunk, embedding)
        log.debug("store_artifact done path=%s chunks=%d", path.name, len(chunks))
    except Exception as e:
        log.warning("store_artifact failed path=%s: %s", file_path, e)


def _chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    chunks, start = [], 0
    while start < len(text):
        chunks.append(text[start:min(start + chunk_size, len(text))])
        start += chunk_size - overlap
    return chunks


async def reset_cache() -> None:
    """Force re-check of vector availability on next call."""
    global _vector_ready
    _vector_ready = None
