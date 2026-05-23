"""
ollama_client.py — async Ollama HTTP client.

IMPORTANT: generate() uses qwen2.5-coder:3b (generation model).
           embed() uses nomic-embed-text (embedding model).

With OLLAMA_MAX_LOADED_MODELS=1, calling generate() after embed()
(or vice versa) causes a model swap (~5-10s). Never call both in the
same request path. See architecture notes in README.
"""

import json
import httpx
from . import config

# Single shared client — connection reuse across requests
_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=config.OLLAMA_HOST,
            timeout=config.OLLAMA_TIMEOUT,
        )
    return _client


async def generate(prompt: str) -> str:
    """
    Call Ollama /api/generate with the generation model.
    Returns response text. Never raises — returns error string on failure.
    Caller should check if response starts with "[" to detect errors.
    """
    try:
        r = await get_client().post(
            "/api/generate",
            json={
                "model":  config.OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature":   0.1,
                    "num_predict":   config.OLLAMA_NUM_PREDICT,
                    "num_ctx":       config.OLLAMA_NUM_CTX,
                },
            },
        )
        r.raise_for_status()
        return r.json().get("response", "").strip()
    except httpx.TimeoutException:
        return "[timeout — prompt may be too long, try a smaller path]"
    except Exception as e:
        return f"[model error: {e}]"


async def embed(text: str) -> list[float] | None:
    """
    Generate an embedding vector using the embed model.
    Returns None on failure so callers can degrade gracefully.
    """
    try:
        r = await get_client().post(
            "/api/embeddings",
            json={
                "model":  config.OLLAMA_EMBED_MODEL,
                "prompt": text,
            },
            timeout=90.0,   # nomic may need to swap in from qwen; allow time for model load
        )
        r.raise_for_status()
        return r.json().get("embedding")
    except Exception:
        return None


async def list_models() -> list[str]:
    """Return list of available model names. Empty list on failure."""
    try:
        r = await get_client().get("/api/tags", timeout=5.0)
        if r.status_code == 200:
            return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        pass
    return []


def parse_json_response(text: str) -> dict:
    """
    Extract first JSON object from model response text.
    Strips markdown code fences (```json ... ```) before parsing.
    Returns empty dict if parsing fails — callers must handle this.
    """
    import re
    # Strip markdown code fences — model often wraps JSON in ```json ... ```
    stripped = re.sub(r"```(?:json)?\s*", "", text).strip()
    for candidate in (stripped, text):
        try:
            match = re.search(r"\{.*\}", candidate, re.DOTALL)
            if match:
                return json.loads(match.group())
        except Exception:
            continue
    return {}
