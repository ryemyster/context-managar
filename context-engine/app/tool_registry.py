"""
tool_registry.py — tool definitions and executors for the agentic loop.

Each tool has:
- A JSON schema in OpenAI/Ollama/MCP-compatible function-calling format
- An async executor that returns a plain string result for the message history

Arcade.dev pattern compliance:
- Tool Description: LLM-optimized (when to call, what it returns, call order hints)
- Dependency Hint: descriptions embed "call X before Y" guidance
- Smart Defaults: path defaults documented; omit = search all repos
- Recovery Guide: errors include the expected format and what was received
- Progressive Detail: read_file supports offset/limit for paging large files
- Health Check: health_check tool verifies engine availability
- Permission Gate: all file paths go through safe_resolve()
- Token-Efficient Response: results truncated to AGENT_TOOL_RESULT_MAX_CHARS
"""

from __future__ import annotations

import json
from typing import Callable

from fastapi import HTTPException

from . import config, ollama_client, supabase_vector
from .logger import log
from .repo_reader import read_file as _read_file
from .repo_reader import rel_path, safe_resolve, walk_repo
from .search_worker import find_in_repo, grep_pattern


def _truncate(text: str) -> str:
    limit = config.AGENT_TOOL_RESULT_MAX_CHARS
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated — {len(text) - limit} chars omitted. Use read_file with offset/limit to page.]"


# ── Tool schemas (OpenAI / Ollama / MCP compatible) ────────────────────────────

_SCAN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "scan_directory",
        "description": (
            "List all code files in a directory. "
            "Call this FIRST when you don't know what files exist — it gives you a map to navigate. "
            "Then call read_file or find_in_code on specific files you discover. "
            "Returns a JSON object with 'files' (list of relative paths) and 'count'. "
            "Path must use 'owner/repo/subdir' format — e.g. 'ryemyster/context-manager/context-engine/app'. "
            "Never pass bare '.' or '/' — it will be rejected."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory to scan. Format: 'owner/repo/subdir'. Required.",
                },
            },
            "required": ["path"],
        },
    },
}

_FIND_SCHEMA = {
    "type": "function",
    "function": {
        "name": "find_in_code",
        "description": (
            "Search for a term or concept across all code files in a path. "
            "Returns matching file paths and the lines that matched. "
            "Use this to locate where a function, pattern, or concept is defined — faster than reading files one by one. "
            "Call scan_directory first if you need to know what directories exist. "
            "Use grep instead if you need precise regex matching. "
            "Returns up to 20 unique files with their first matching line. "
            "Path is optional — omit to search across all repos (slower)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Space-separated search terms. Matches any term. Example: 'rate limit middleware'.",
                },
                "path": {
                    "type": "string",
                    "description": "Scope the search. Format: 'owner/repo/subdir'. Optional.",
                },
            },
            "required": ["query"],
        },
    },
}

_READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read the contents of a specific file. "
            "Use this after scan_directory or find_in_code to inspect a file you've identified as relevant. "
            "Output is truncated if the file is large — use offset and limit to page through it. "
            "Always call scan_directory or find_in_code first so you know the file exists. "
            "Path must use 'owner/repo/path/to/file.ext' format."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "File path from repo root. Format: 'owner/repo/path/to/file.py'. Required.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Line number to start reading from (1-indexed). Default: 1.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max lines to return. Default: all. Use with offset to page large files.",
                },
            },
            "required": ["file"],
        },
    },
}

_GREP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "grep",
        "description": (
            "Search for a regex pattern across files in a path, returning all matching lines. "
            "Use for precise pattern matching: function signatures, import statements, specific strings. "
            "For concept/keyword search, prefer find_in_code. "
            "Pattern is case-insensitive regex. Returns 'filepath: matching line' per hit."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Case-insensitive regex. Example: 'def run_agent|async def run'.",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search. Format: 'owner/repo/subdir'. Optional.",
                },
            },
            "required": ["pattern"],
        },
    },
}

_HEALTH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "health_check",
        "description": (
            "Verify the context engine is healthy and models are available. "
            "Call this at session start before a long task to confirm the engine is operational."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

_SEARCH_MEMORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_memory",
        "description": (
            "Search indexed artifacts and prior agent run results from memory. "
            "Call this FIRST when starting a task that may have been investigated before — "
            "before calling scan_directory or find_in_code. "
            "Returns matching chunks with similarity scores from prior runs, summaries, and indexed code. "
            "If results are found, use them as a starting point instead of re-scanning from scratch."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language description of what you are looking for.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return. Default: 5. Max: 10.",
                },
            },
            "required": ["query"],
        },
    },
}


# ── Executors ──────────────────────────────────────────────────────────────────

async def _exec_scan_directory(arguments: dict) -> str:
    path = arguments.get("path", "")
    if not path:
        return "[error: 'path' is required — use format 'owner/repo/subdir']"
    try:
        base = safe_resolve(path)
    except HTTPException as e:
        return f"[error: path rejected — {e.detail}. Expected format: 'owner/repo/subdir', got '{path}']"
    except Exception as e:
        return f"[error: {e}]"
    files = walk_repo(base)
    paths = [rel_path(f) for f in files]
    result = json.dumps({"path": path, "files": paths, "count": len(paths)}, indent=2)
    return _truncate(result)


async def _exec_find_in_code(arguments: dict) -> str:
    query = arguments.get("query", "")
    path = arguments.get("path", ".")
    if not query:
        return "[error: 'query' is required]"
    try:
        matches = find_in_repo(query, base_path=path)
    except HTTPException as e:
        return f"[error: path rejected — {e.detail}. Expected format: 'owner/repo/subdir', got '{path}']"
    except Exception as e:
        return f"[error: {e}]"
    if not matches:
        return f"[no matches for '{query}' in '{path}']"
    lines = [f"{m['path']}:{m['line_no']}: {m['line'][:120]}" for m in matches]
    return _truncate("\n".join(lines))


async def _exec_read_file(arguments: dict) -> str:
    file = arguments.get("file", "")
    offset = int(arguments.get("offset") or 1)
    limit = arguments.get("limit")
    if not file:
        return "[error: 'file' is required — use format 'owner/repo/path/to/file.py']"
    try:
        resolved = safe_resolve(file)
    except HTTPException as e:
        return f"[error: path rejected — {e.detail}. Expected format: 'owner/repo/path/to/file.py', got '{file}']"
    except Exception as e:
        return f"[error: {e}]"
    # When paging past the first block, request enough bytes to cover the offset.
    # repo_reader caps at MAX_FILE_BYTES (32KB) by default — too small for large files.
    needed_bytes = max(config.MAX_FILE_BYTES, (offset + int(limit or 200)) * 300)
    content = _read_file(resolved, max_bytes=min(needed_bytes, 2_000_000))
    if not content:
        return f"[error: file not found or unreadable: '{file}']"
    lines = content.splitlines()
    start = max(0, offset - 1)
    end = (start + int(limit)) if limit else len(lines)
    sliced = "\n".join(lines[start:end])
    return _truncate(sliced)


async def _exec_grep(arguments: dict) -> str:
    pattern = arguments.get("pattern", "")
    path = arguments.get("path", ".")
    if not pattern:
        return "[error: 'pattern' is required]"
    try:
        base = safe_resolve(path) if path and path != "." else config.REPO_ROOT
    except HTTPException as e:
        return f"[error: path rejected — {e.detail}. Expected format: 'owner/repo/subdir', got '{path}']"
    except Exception as e:
        return f"[error: {e}]"
    files = walk_repo(base)
    results: list[str] = []
    for f in files:
        content = _read_file(f)
        if not content:
            continue
        hits = grep_pattern(content, pattern)
        for hit in hits:
            results.append(f"{rel_path(f)}: {hit}")
        if len(results) >= config.MAX_SNIPPETS_PER_QUERY:
            break
    if not results:
        return f"[no matches for pattern '{pattern}' in '{path}']"
    return _truncate("\n".join(results))


async def _exec_health_check(arguments: dict) -> str:
    try:
        import httpx as _httpx
        r = _httpx.get("http://localhost:8088/healthcheck", timeout=3.0)
        return f"health: {'ok' if r.status_code == 200 else 'degraded'} (HTTP {r.status_code})"
    except Exception as e:
        return f"[health_check error: {e}]"


async def _exec_search_memory(arguments: dict) -> str:
    query = arguments.get("query", "").strip()
    if not query:
        return "[error: 'query' is required]"
    limit = min(int(arguments.get("limit", 5)), 10)
    try:
        if not await supabase_vector.is_available():
            return "[no memory found for this query]"
        embedding = await ollama_client.embed(query)
        results = await supabase_vector.search(embedding, limit=limit, threshold=0.3)
    except Exception as exc:
        return f"[error: search_memory failed — {exc}]"
    if not results:
        return "[no memory found for this query]"
    parts = [
        f"[{r['similarity']:.2f}] {r['path']}\n{r['chunk']}"
        for r in results
    ]
    return _truncate("\n\n---\n\n".join(parts))


# ── Registry ───────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, tuple[dict, Callable]] = {
    "scan_directory": (_SCAN_SCHEMA,        _exec_scan_directory),
    "find_in_code":   (_FIND_SCHEMA,        _exec_find_in_code),
    "read_file":      (_READ_SCHEMA,        _exec_read_file),
    "grep":           (_GREP_SCHEMA,        _exec_grep),
    "health_check":   (_HEALTH_SCHEMA,      _exec_health_check),
    "search_memory":  (_SEARCH_MEMORY_SCHEMA, _exec_search_memory),
}

ALL_TOOLS: list[str] = list(_REGISTRY.keys())


def get_tool_definitions(names: list[str] | None = None) -> list[dict]:
    """Return Ollama/MCP-compatible tool definition list for the requested tools."""
    keys = names if names else ALL_TOOLS
    return [_REGISTRY[k][0] for k in keys if k in _REGISTRY]


async def execute_tool(name: str, arguments: dict) -> str:
    """Invoke a named tool and return its plain-string result for the agent message history."""
    if name not in _REGISTRY:
        log.warning("tool_registry unknown tool '%s'", name)
        return f"[error: unknown tool '{name}'. Available: {', '.join(ALL_TOOLS)}]"
    _, executor = _REGISTRY[name]
    try:
        return await executor(arguments)
    except Exception as e:
        log.error("tool_registry execute_tool name=%s error: %s", name, e)
        return f"[error executing '{name}': {e}]"
