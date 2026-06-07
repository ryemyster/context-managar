"""
tool_registry.py — tool definitions and executors for the agentic loop.

Each tool has:
- A JSON schema in OpenAI/Ollama/MCP-compatible function-calling format
- An async executor that returns a ToolResult
- A ToolMeta declaring its scope requirements and side-effect profile

Arcade.dev pattern compliance:
- Tool Description: LLM-optimized (when to call, what it returns, call order hints)
- Dependency Hint: descriptions embed "call X before Y" guidance
- Smart Defaults: path defaults documented; omit = search all repos
- Recovery Guide: errors include error_type, retryable flag, and recovery_hint
- Progressive Detail: read_file supports offset/limit for paging large files
- Health Check: health_check tool verifies engine availability
- Permission Gate: all file paths go through safe_resolve()
- Token-Efficient Response: results truncated to AGENT_TOOL_RESULT_MAX_CHARS
- Scope Enforcement: each tool declares required scopes; execute_tool enforces them
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from fastapi import HTTPException

from . import config, ollama_client, supabase_vector
from .logger import log
from .repo_reader import read_file as _read_file
from .repo_reader import rel_path, safe_resolve, walk_repo
from .search_worker import find_in_repo, grep_pattern


# ── Result envelope ────────────────────────────────────────────────────────────

@dataclass
class ToolResult:
    ok: bool
    data: str
    error_type: str | None = None   # "path_rejected" | "not_found" | "invalid_input" | "engine_down" | "scope_denied"
    retryable: bool = False
    recovery_hint: str | None = None


# ── Tool metadata / scopes ─────────────────────────────────────────────────────

SCOPE_REPO_READ   = "repo:read"
SCOPE_MEMORY_READ = "memory:read"
SCOPE_ENGINE_READ = "engine:read"

@dataclass
class ToolMeta:
    scopes: list[str] = field(default_factory=list)
    side_effects: bool = False


# ── Helpers ────────────────────────────────────────────────────────────────────

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
            "before calling update_plan, scan_directory or find_in_code. "
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

_PLAN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_plan",
        "description": (
            "Record your current plan before starting exploration. "
            "Call this after search_memory and before any scan/read/grep calls. "
            "Stores: goal, numbered steps, current step index, and any known blockers. "
            "Call again to revise when the plan changes. "
            "The plan is persisted with the run and visible to callers."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "One-sentence statement of what you are trying to accomplish.",
                },
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ordered list of steps to complete the goal.",
                },
                "current_step": {
                    "type": "integer",
                    "description": "0-based index of the step currently being executed. Default: 0.",
                },
                "blockers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Any blockers or unknowns that may affect the plan. Optional.",
                },
            },
            "required": ["goal", "steps"],
        },
    },
}


# ── Executors ──────────────────────────────────────────────────────────────────

async def _exec_scan_directory(arguments: dict) -> ToolResult:
    path = arguments.get("path", "")
    if not path:
        return ToolResult(ok=False, data="'path' is required",
                          error_type="invalid_input",
                          recovery_hint="Use format 'owner/repo/subdir'")
    try:
        base = safe_resolve(path)
    except HTTPException as e:
        return ToolResult(ok=False, data=f"path rejected — {e.detail}",
                          error_type="path_rejected",
                          recovery_hint=f"Expected 'owner/repo/subdir', got '{path}'")
    except Exception as e:
        return ToolResult(ok=False, data=str(e), error_type="engine_down", retryable=True)
    files = walk_repo(base)
    paths = [rel_path(f) for f in files]
    result = json.dumps({"path": path, "files": paths, "count": len(paths)}, indent=2)
    return ToolResult(ok=True, data=_truncate(result))


async def _exec_find_in_code(arguments: dict) -> ToolResult:
    query = arguments.get("query", "")
    path = arguments.get("path", ".")
    if not query:
        return ToolResult(ok=False, data="'query' is required", error_type="invalid_input")
    try:
        matches = find_in_repo(query, base_path=path)
    except HTTPException as e:
        return ToolResult(ok=False, data=f"path rejected — {e.detail}",
                          error_type="path_rejected",
                          recovery_hint=f"Expected 'owner/repo/subdir', got '{path}'")
    except Exception as e:
        return ToolResult(ok=False, data=str(e), error_type="engine_down", retryable=True)
    if not matches:
        return ToolResult(ok=True, data=f"[no matches for '{query}' in '{path}']")
    lines = [f"{m['path']}:{m['line_no']}: {m['line'][:120]}" for m in matches]
    return ToolResult(ok=True, data=_truncate("\n".join(lines)))


async def _exec_read_file(arguments: dict) -> ToolResult:
    file = arguments.get("file", "")
    offset = int(arguments.get("offset") or 1)
    limit = arguments.get("limit")
    if not file:
        return ToolResult(ok=False, data="'file' is required",
                          error_type="invalid_input",
                          recovery_hint="Use format 'owner/repo/path/to/file.py'")
    try:
        resolved = safe_resolve(file)
    except HTTPException as e:
        return ToolResult(ok=False, data=f"path rejected — {e.detail}",
                          error_type="path_rejected",
                          recovery_hint=f"Expected 'owner/repo/path/to/file.py', got '{file}'")
    except Exception as e:
        return ToolResult(ok=False, data=str(e), error_type="engine_down", retryable=True)
    # When paging past the first block, request enough bytes to cover the offset.
    needed_bytes = max(config.MAX_FILE_BYTES, (offset + int(limit or 200)) * 300)
    content = _read_file(resolved, max_bytes=min(needed_bytes, 2_000_000))
    if not content:
        return ToolResult(ok=False, data=f"file not found or unreadable: '{file}'",
                          error_type="not_found")
    lines = content.splitlines()
    start = max(0, offset - 1)
    end = (start + int(limit)) if limit else len(lines)
    sliced = "\n".join(lines[start:end])
    return ToolResult(ok=True, data=_truncate(sliced))


async def _exec_grep(arguments: dict) -> ToolResult:
    pattern = arguments.get("pattern", "")
    path = arguments.get("path", ".")
    if not pattern:
        return ToolResult(ok=False, data="'pattern' is required", error_type="invalid_input")
    try:
        base = safe_resolve(path) if path and path != "." else config.REPO_ROOT
    except HTTPException as e:
        return ToolResult(ok=False, data=f"path rejected — {e.detail}",
                          error_type="path_rejected",
                          recovery_hint=f"Expected 'owner/repo/subdir', got '{path}'")
    except Exception as e:
        return ToolResult(ok=False, data=str(e), error_type="engine_down", retryable=True)
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
        return ToolResult(ok=True, data=f"[no matches for pattern '{pattern}' in '{path}']")
    return ToolResult(ok=True, data=_truncate("\n".join(results)))


async def _exec_health_check(arguments: dict) -> ToolResult:
    try:
        import httpx as _httpx
        r = _httpx.get("http://localhost:8088/healthcheck", timeout=3.0)
        status = "ok" if r.status_code == 200 else "degraded"
        return ToolResult(ok=r.status_code == 200,
                          data=f"health: {status} (HTTP {r.status_code})",
                          error_type=None if r.status_code == 200 else "engine_down",
                          retryable=r.status_code != 200)
    except Exception as e:
        return ToolResult(ok=False, data=f"health_check failed: {e}",
                          error_type="engine_down", retryable=True)


async def _exec_search_memory(arguments: dict) -> ToolResult:
    query = arguments.get("query", "").strip()
    if not query:
        return ToolResult(ok=False, data="'query' is required", error_type="invalid_input")
    limit = min(int(arguments.get("limit", 5)), 10)
    try:
        if not await supabase_vector.is_available():
            return ToolResult(ok=True, data="[no memory found for this query]")
        embedding = await ollama_client.embed(query)
        results = await supabase_vector.search(embedding, limit=limit, threshold=0.3)
    except Exception as exc:
        return ToolResult(ok=False, data=f"search_memory failed — {exc}",
                          error_type="engine_down", retryable=True)
    if not results:
        return ToolResult(ok=True, data="[no memory found for this query]")
    parts = [
        f"[{r['similarity']:.2f}] {r['path']}\n{r['chunk']}"
        for r in results
    ]
    return ToolResult(ok=True, data=_truncate("\n\n---\n\n".join(parts)))


async def _exec_update_plan(arguments: dict) -> ToolResult:
    goal  = arguments.get("goal", "").strip()
    steps = arguments.get("steps") or []
    if not goal or not steps:
        return ToolResult(ok=False, data="'goal' and 'steps' are required",
                          error_type="invalid_input")
    plan = {
        "goal":         goal,
        "steps":        list(steps),
        "current_step": int(arguments.get("current_step", 0)),
        "blockers":     list(arguments.get("blockers") or []),
    }
    return ToolResult(ok=True, data=json.dumps(plan))


# ── Registry ───────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, tuple[dict, Callable, ToolMeta]] = {
    "scan_directory": (_SCAN_SCHEMA,          _exec_scan_directory, ToolMeta(scopes=[SCOPE_REPO_READ])),
    "find_in_code":   (_FIND_SCHEMA,          _exec_find_in_code,   ToolMeta(scopes=[SCOPE_REPO_READ])),
    "read_file":      (_READ_SCHEMA,          _exec_read_file,      ToolMeta(scopes=[SCOPE_REPO_READ])),
    "grep":           (_GREP_SCHEMA,          _exec_grep,           ToolMeta(scopes=[SCOPE_REPO_READ])),
    "health_check":   (_HEALTH_SCHEMA,        _exec_health_check,   ToolMeta(scopes=[SCOPE_ENGINE_READ])),
    "search_memory":  (_SEARCH_MEMORY_SCHEMA, _exec_search_memory,  ToolMeta(scopes=[SCOPE_MEMORY_READ])),
    "update_plan":    (_PLAN_SCHEMA,          _exec_update_plan,    ToolMeta(scopes=[])),
}

ALL_TOOLS: list[str] = list(_REGISTRY.keys())


def get_tool_definitions(names: list[str] | None = None) -> list[dict]:
    """Return Ollama/MCP-compatible tool definition list for the requested tools."""
    keys = names if names else ALL_TOOLS
    return [_REGISTRY[k][0] for k in keys if k in _REGISTRY]


def get_tool_metadata(name: str) -> ToolMeta | None:
    """Return ToolMeta for a named tool, or None if unknown."""
    entry = _REGISTRY.get(name)
    return entry[2] if entry else None


async def execute_tool(
    name: str,
    arguments: dict,
    allowed_scopes: list[str] | None = None,
) -> ToolResult:
    """
    Invoke a named tool and return a ToolResult.

    allowed_scopes: if provided, tools whose scopes are not covered will be denied.
    None means all scopes are permitted.
    """
    if name not in _REGISTRY:
        log.warning("tool_registry unknown tool '%s'", name)
        return ToolResult(
            ok=False,
            data=f"unknown tool '{name}'",
            error_type="invalid_input",
            recovery_hint=f"Available tools: {', '.join(ALL_TOOLS)}",
        )

    _, executor, meta = _REGISTRY[name]

    if allowed_scopes is not None and meta.scopes:
        if not any(s in allowed_scopes for s in meta.scopes):
            log.debug("tool_registry scope_denied tool=%s required=%s allowed=%s",
                      name, meta.scopes, allowed_scopes)
            return ToolResult(
                ok=False,
                data=f"tool '{name}' requires scope {meta.scopes}",
                error_type="scope_denied",
                retryable=False,
                recovery_hint=f"Add one of {meta.scopes} to allowed_scopes",
            )

    try:
        return await executor(arguments)
    except Exception as e:
        log.error("tool_registry execute_tool name=%s error: %s", name, e)
        return ToolResult(ok=False, data=str(e), error_type="engine_down", retryable=True)
