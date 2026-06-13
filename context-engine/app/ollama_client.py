"""
ollama_client.py — async Ollama HTTP client.

IMPORTANT: generate()           uses qwen2.5-coder:3b (code model).
           generate_reasoning() uses qwen3.5:9b (reasoning model).
           embed()              uses nomic-embed-text (embedding model).

With OLLAMA_MAX_LOADED_MODELS=1, calling generate() after embed()
(or vice versa) causes a model swap (~5-10s). Never call both in the
same request path. See architecture notes in README.
"""

import json
import time
import httpx
from . import config
from .logger import log

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
    log.debug("ollama generate model=%s prompt_len=%d", config.OLLAMA_MODEL, len(prompt))
    log.trace("ollama generate prompt=%r", prompt[:500])
    t0 = time.monotonic()
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
            timeout=config.OLLAMA_TIMEOUT,
        )
        r.raise_for_status()
        result = r.json().get("response", "").strip()
        log.debug("ollama generate done dur=%.2fs response_len=%d", time.monotonic() - t0, len(result))
        log.trace("ollama generate response=%r", result[:500])
        return result
    except httpx.TimeoutException:
        log.warning("ollama generate timeout model=%s dur=%.2fs prompt_len=%d", config.OLLAMA_MODEL, time.monotonic() - t0, len(prompt))
        return "[timeout — prompt may be too long, try a smaller path]"
    except Exception as e:
        log.error("ollama generate error: %s", e)
        return f"[model error: {e}]"


async def generate_reasoning(prompt: str) -> str:
    """
    Call Ollama /api/generate with the reasoning model (qwen3.5:9b).
    Use for higher-level architectural analysis, not hot-path code tasks.
    Returns error string on failure — caller checks for leading "[".
    """
    log.debug("ollama reasoning model=%s prompt_len=%d", config.OLLAMA_REASON_MODEL, len(prompt))
    log.trace("ollama reasoning prompt=%r", prompt[:500])
    t0 = time.monotonic()
    try:
        r = await get_client().post(
            "/api/generate",
            json={
                "model":  config.OLLAMA_REASON_MODEL,
                "prompt": prompt,
                "stream": False,
                "think":  False,
                "options": {
                    "temperature":   0.2,
                    "num_predict":   config.OLLAMA_REASON_PREDICT,
                    "num_ctx":       config.OLLAMA_NUM_CTX,
                },
            },
            timeout=config.OLLAMA_REASON_TIMEOUT,
        )
        r.raise_for_status()
        result = r.json().get("response", "").strip()
        log.debug("ollama reasoning done dur=%.2fs response_len=%d", time.monotonic() - t0, len(result))
        log.trace("ollama reasoning response=%r", result[:500])
        return result
    except httpx.TimeoutException:
        log.warning("ollama reasoning timeout model=%s dur=%.2fs", config.OLLAMA_REASON_MODEL, time.monotonic() - t0)
        return "[timeout — reasoning prompt may be too long, try a smaller path]"
    except Exception as e:
        log.error("ollama reasoning error: %s", e)
        return f"[model error: {e}]"


async def embed(text: str) -> list[float] | None:
    """
    Generate an embedding vector using the embed model.
    Returns None on failure so callers can degrade gracefully.
    """
    log.debug("ollama embed model=%s text_len=%d", config.OLLAMA_EMBED_MODEL, len(text))
    t0 = time.monotonic()
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
        result = r.json().get("embedding")
        log.debug("ollama embed done dur=%.2fs dims=%d", time.monotonic() - t0, len(result) if result else 0)
        return result
    except Exception as e:
        log.warning("ollama embed failed dur=%.2fs: %s", time.monotonic() - t0, e)
        return None


async def chat_with_tools(
    messages: list[dict],
    tools: list[dict],
    model: str | None = None,
    timeout: float | None = None,
) -> dict:
    """
    Call Ollama /api/chat with tool definitions (uses the agent model by default).

    Returns the raw message dict from the response:
    - message.tool_calls present  → model wants to call a tool
    - message.tool_calls absent   → model has a final answer in message.content

    Never raises — returns {"error": str} so agent_runner can detect and stop the loop.
    """
    use_model = model or config.OLLAMA_AGENT_SELECT_MODEL
    request_timeout = timeout or config.OLLAMA_AGENT_CALL_TIMEOUT
    log.debug("ollama chat_with_tools model=%s messages=%d tools=%d", use_model, len(messages), len(tools))
    t0 = time.monotonic()
    try:
        r = await get_client().post(
            "/api/chat",
            json={
                "model":    use_model,
                "messages": messages,
                "tools":    tools,
                "stream":   False,
                "think":    False,
                "options": {
                    "temperature": 0.1,
                    "num_ctx":     config.OLLAMA_NUM_CTX,
                    "num_predict": config.OLLAMA_AGENT_NUM_PREDICT,
                },
            },
            timeout=request_timeout,
        )
        r.raise_for_status()
        message = r.json().get("message", {})
        log.debug("ollama chat_with_tools done dur=%.2fs has_tool_calls=%s",
                  time.monotonic() - t0, bool(message.get("tool_calls")))
        return message
    except httpx.TimeoutException:
        log.warning("ollama chat_with_tools timeout model=%s dur=%.2fs", use_model, time.monotonic() - t0)
        return {"error": f"timeout after {request_timeout:.0f}s — model took too long"}
    except Exception as e:
        log.error("ollama chat_with_tools error: %s", e)
        return {"error": str(e)}


async def select_tool_call(
    messages: list[dict],
    tools: list[dict],
    model: str | None = None,
    timeout: float | None = None,
) -> dict:
    """Select the next agent action through schema-constrained JSON."""
    use_model = model or config.OLLAMA_AGENT_MODEL
    request_timeout = timeout or config.OLLAMA_AGENT_SELECT_TIMEOUT
    names = [
        tool.get("function", {}).get("name")
        for tool in tools
        if tool.get("function", {}).get("name")
    ]
    if not names:
        return {"error": "no enabled tools"}

    conversation = []
    for message in messages[-8:]:
        content = str(message.get("content") or "")
        conversation.append({
            "role": message.get("role", ""),
            "content": content[:2500],
        })
    compact_tools = [
        {
            "name": tool["function"]["name"],
            "description": tool["function"].get("description", ""),
            "parameters": tool["function"].get("parameters", {}),
        }
        for tool in tools
        if tool.get("function", {}).get("name")
    ]
    prompt = (
        f"Conversation and evidence:\n{json.dumps(conversation, separators=(',', ':'))}\n\n"
        f"Enabled tools:\n{json.dumps(compact_tools, separators=(',', ':'))}\n\n"
        "Choose the next action. Use call_tool only when more repository evidence "
        "is required. Use final_answer when the existing tool evidence is enough. "
        "Never repeat an identical successful tool call."
    )
    try:
        r = await get_client().post(
            "/api/chat",
            json={
                "model": use_model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Choose the next repository-agent action. Return only "
                            "the JSON object required by the response schema."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "format": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["call_tool", "final_answer"],
                        },
                        "name": {"type": "string", "enum": names},
                        "arguments": {"type": "object"},
                        "final_answer": {"type": "string"},
                    },
                    "required": ["action", "name", "arguments", "final_answer"],
                },
                "stream": False,
                "think": False,
                "options": {
                    "temperature": 0,
                    "num_ctx": config.OLLAMA_NUM_CTX,
                    "num_predict": 200,
                },
            },
            timeout=request_timeout,
        )
        r.raise_for_status()
        parsed = parse_json_response(r.json().get("message", {}).get("content", ""))
        if parsed.get("action") == "final_answer":
            final_answer = str(parsed.get("final_answer") or "").strip()
            if final_answer:
                return {"final_answer": final_answer}
            return {"error": "structured action returned an empty final answer"}
        name = parsed.get("name")
        arguments = parsed.get("arguments")
        if parsed.get("action") != "call_tool" or name not in names or not isinstance(arguments, dict):
            return {"error": "structured tool selection returned an invalid call"}
        return {"function": {"name": name, "arguments": arguments}}
    except httpx.TimeoutException:
        return {"error": f"structured tool selection timed out after {request_timeout:.0f}s"}
    except Exception as e:
        return {"error": str(e)}


async def verify_agent_answer(
    prompt: str,
    model: str | None = None,
    timeout: float | None = None,
) -> dict:
    """Run the agent evidence check with schema-constrained JSON."""
    use_model = model or config.OLLAMA_AGENT_VERIFY_MODEL
    request_timeout = timeout or config.OLLAMA_AGENT_VERIFY_TIMEOUT
    try:
        r = await get_client().post(
            "/api/chat",
            json={
                "model": use_model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Evaluate whether the answer is supported by the "
                            "provided tool evidence. Return only the required JSON."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "format": {
                    "type": "object",
                    "properties": {
                        "passed": {"type": "boolean"},
                        "rationale": {"type": "string"},
                        "unsupported_claims": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "evidence_gap": {"type": "boolean"},
                    },
                    "required": [
                        "passed",
                        "rationale",
                        "unsupported_claims",
                        "evidence_gap",
                    ],
                },
                "stream": False,
                "think": False,
                "options": {
                    "temperature": 0,
                    "num_ctx": config.OLLAMA_NUM_CTX,
                    "num_predict": 200,
                },
            },
            timeout=request_timeout,
        )
        r.raise_for_status()
        parsed = parse_json_response(r.json().get("message", {}).get("content", ""))
        if "passed" not in parsed:
            return {"error": "parse_failed"}
        return parsed
    except httpx.TimeoutException:
        return {"error": "verifier_timeout"}
    except Exception as e:
        return {"error": str(e)}


async def answer_from_evidence(
    messages: list[dict],
    model: str | None = None,
    timeout: float | None = None,
) -> dict:
    """Produce a final answer from accumulated tool evidence without exposing tools."""
    use_model = model or config.OLLAMA_AGENT_MODEL
    request_timeout = timeout or config.OLLAMA_AGENT_SELECT_TIMEOUT
    conversation = [
        {
            "role": message.get("role", ""),
            "content": str(message.get("content") or "")[:3000],
        }
        for message in messages[-8:]
    ]
    try:
        r = await get_client().post(
            "/api/chat",
            json={
                "model": use_model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Answer the repository task using only the accumulated "
                            "tool evidence. Be concise and cite repository-relative paths. "
                            "Return only the required JSON."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(conversation, separators=(",", ":")),
                    },
                ],
                "format": {
                    "type": "object",
                    "properties": {"final_answer": {"type": "string"}},
                    "required": ["final_answer"],
                },
                "stream": False,
                "think": False,
                "options": {
                    "temperature": 0,
                    "num_ctx": config.OLLAMA_NUM_CTX,
                    "num_predict": config.OLLAMA_AGENT_NUM_PREDICT,
                },
            },
            timeout=request_timeout,
        )
        r.raise_for_status()
        parsed = parse_json_response(r.json().get("message", {}).get("content", ""))
        answer = str(parsed.get("final_answer") or "").strip()
        return {"final_answer": answer} if answer else {"error": "empty final answer"}
    except httpx.TimeoutException:
        return {"error": f"answer synthesis timed out after {request_timeout:.0f}s"}
    except Exception as e:
        return {"error": str(e)}


async def list_models() -> list[str]:
    """Return list of available model names. Empty list on failure."""
    try:
        r = await get_client().get("/api/tags", timeout=5.0)
        if r.status_code == 200:
            return [m["name"] for m in r.json().get("models", [])]
    except Exception as e:
        log.debug("ollama list_models failed: %s", e)
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
