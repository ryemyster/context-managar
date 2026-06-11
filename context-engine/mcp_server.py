#!/usr/bin/env python3
"""
Thin MCP stdio adapter for the existing context-engine REST API.

The MCP server owns transport only. Planning, repository exploration, memory,
verification, repair passes, and evidence collection remain in context-engine.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

import httpx


ENGINE_BASE = os.getenv("CONTEXT_ENGINE_URL", "http://localhost:8088").rstrip("/")
ENGINE_API_KEY = os.getenv("CONTEXT_ENGINE_API_KEY", "")
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "context-engine"
SERVER_VERSION = "2.0.0"

REQUEST_TIMEOUT = float(os.getenv("CONTEXT_ENGINE_MCP_REQUEST_TIMEOUT", "120"))
RUN_TIMEOUT = float(os.getenv("CONTEXT_ENGINE_MCP_RUN_TIMEOUT", "900"))
POLL_INTERVAL = float(os.getenv("CONTEXT_ENGINE_MCP_POLL_INTERVAL", "1"))


def _schema(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "annotations": {
            "title": name.replace("_", " ").title(),
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    }


TOOLS = [
    _schema(
        "investigate_codebase",
        (
            "PRIMARY DELEGATION TOOL. Delegate a repository investigation to the "
            "Context Engine junior engineer. Prefer this for codebase questions "
            "instead of manually chaining scan, search, summarize, or dependency "
            "tools. The existing agent creates and updates a plan, searches memory, "
            "scans and reads repository files, verifies its answer, runs repair "
            "passes when needed, and returns an evidence trail. This adapter starts "
            "POST /agents/run and polls until completion."
        ),
        {
            "task": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "A bounded delegation request. Include repository-relative paths, "
                    "focus symbols, and a numbered output contract when known."
                ),
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
                "description": "Optional internal-agent tool allowlist. Empty enables all.",
            },
            "max_iterations": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 10,
            },
            "system_prompt": {
                "type": ["string", "null"],
                "default": None,
                "description": "Optional override for the existing junior agent prompt.",
            },
            "allowed_scopes": {
                "type": ["array", "null"],
                "items": {
                    "type": "string",
                    "enum": ["repo:read", "memory:read", "engine:read"],
                },
                "default": None,
                "description": "Optional scope restriction. Null permits all read scopes.",
            },
        },
        ["task"],
    ),
    _schema(
        "load_context",
        (
            "Gather a focused context bundle before implementation by calling the "
            "existing POST /context workflow. Use this when the caller needs a fast "
            "pre-task inventory. For open-ended investigation or conclusions that "
            "need an evidence trail, prefer investigate_codebase."
        ),
        {
            "task": {"type": "string", "minLength": 1},
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
                "description": "Scoped paths using the owner/repo/subpath convention.",
            },
            "focus": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
            "use_vector": {"type": "boolean", "default": False},
        },
        ["task"],
    ),
    _schema(
        "review_diff",
        (
            "Review a git diff through the existing POST /diff-summary workflow. "
            "Returns the summary, risks, touched files, and test recommendations. "
            "Use after edits; do not perform diff analysis in the MCP adapter."
        ),
        {
            "diff": {
                "type": "string",
                "minLength": 1,
                "description": "Raw git diff text.",
            },
        },
        ["diff"],
    ),
    _schema(
        "audit_issue",
        (
            "Delegate evidence-based issue investigation to the existing issue "
            "auditor. The adapter calls POST /agents/issue-auditor/run and polls its "
            "status endpoint. Context Engine gathers evidence; the senior engineer "
            "retains final issue and GitHub decisions."
        ),
        {
            "task": {"type": "string", "minLength": 1},
            "repo": {
                "type": "string",
                "minLength": 1,
                "description": "Repository prefix, for example ryemyster/context-manager.",
            },
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            },
            "focus": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
            "requirements": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
            "use_vector": {"type": "boolean", "default": False},
        },
        ["task", "repo", "paths"],
    ),
    _schema(
        "scan_directory",
        (
            "ADVANCED DIRECT TOOL. Call the existing POST /scan workflow for a "
            "directory inventory. Do not manually orchestrate this with other "
            "advanced tools when investigate_codebase can own the investigation."
        ),
        {
            "path": {
                "type": "string",
                "minLength": 1,
                "description": "Scoped owner/repo/subpath directory.",
            },
        },
        ["path"],
    ),
    _schema(
        "find_in_code",
        (
            "ADVANCED DIRECT TOOL. Call the existing POST /find workflow to locate "
            "a concept. Prefer investigate_codebase when the result must be "
            "interpreted, verified, or combined with other repository evidence."
        ),
        {
            "query": {"type": "string", "minLength": 1},
            "path": {
                "type": "string",
                "default": ".",
                "description": "Optional owner/repo/subpath search scope.",
            },
        },
        ["query"],
    ),
    _schema(
        "summarize_file",
        (
            "ADVANCED DIRECT TOOL. Call the existing POST /summarize workflow for "
            "one known file. Prefer investigate_codebase when discovery or "
            "cross-file reasoning is required."
        ),
        {
            "file": {
                "type": "string",
                "minLength": 1,
                "description": "Scoped owner/repo/path/to/file.",
            },
        },
        ["file"],
    ),
    _schema(
        "dependency_analysis",
        (
            "ADVANCED DIRECT TOOL. Call the existing POST /dependencies workflow "
            "for an import graph. Prefer investigate_codebase for architectural "
            "conclusions or multi-step investigation."
        ),
        {
            "path": {
                "type": "string",
                "minLength": 1,
                "description": "Scoped owner/repo/subpath.",
            },
        },
        ["path"],
    ),
    _schema(
        "vector_search",
        (
            "ADVANCED DIRECT TOOL. Call the existing POST /vector-search workflow "
            "for semantically similar indexed chunks. This is a retrieval primitive, "
            "not a replacement for investigate_codebase."
        ),
        {
            "query": {"type": "string", "minLength": 1},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 8,
            },
            "threshold": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "default": 0.3,
            },
        },
        ["query"],
    ),
]

TOOL_NAMES = {tool["name"] for tool in TOOLS}


class AdapterError(Exception):
    """Expected MCP tool failure that should be returned with isError=true."""


def _ok(id_: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _err(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _write(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if ENGINE_API_KEY:
        headers["X-API-Key"] = ENGINE_API_KEY
    return headers


async def _request_json(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(
            base_url=ENGINE_BASE,
            headers=_headers(),
            timeout=timeout or REQUEST_TIMEOUT,
        ) as client:
            response = await client.request(method, path, json=payload)
    except httpx.TimeoutException as exc:
        raise AdapterError(f"context-engine request timed out: {method} {path}") from exc
    except httpx.HTTPError as exc:
        raise AdapterError(f"context-engine is unreachable at {ENGINE_BASE}: {exc}") from exc

    if response.status_code >= 400:
        detail = response.text.strip()
        try:
            detail = response.json().get("detail", detail)
        except (ValueError, AttributeError):
            pass
        raise AdapterError(
            f"context-engine returned HTTP {response.status_code} for {path}: {detail}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise AdapterError(f"context-engine returned non-JSON data for {path}") from exc
    if not isinstance(data, dict):
        raise AdapterError(f"context-engine returned an unexpected response for {path}")
    return data


async def _poll_run(status_path: str, initial: dict[str, Any]) -> dict[str, Any]:
    run_id = initial.get("run_id")
    if not run_id:
        raise AdapterError("context-engine did not return a run_id")

    deadline = time.monotonic() + RUN_TIMEOUT
    result = initial
    while result.get("status") == "running":
        if time.monotonic() >= deadline:
            raise AdapterError(
                f"context-engine run {run_id} did not finish within {RUN_TIMEOUT:.0f}s"
            )
        await asyncio.sleep(POLL_INTERVAL)
        result = await _request_json("GET", status_path.format(run_id=run_id))
    return result


async def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "investigate_codebase":
        initial = await _request_json("POST", "/agents/run", payload=arguments)
        return await _poll_run("/agents/run/status/{run_id}", initial)
    if name == "load_context":
        return await _request_json("POST", "/context", payload=arguments)
    if name == "review_diff":
        return await _request_json("POST", "/diff-summary", payload=arguments)
    if name == "audit_issue":
        initial = await _request_json(
            "POST", "/agents/issue-auditor/run", payload=arguments
        )
        return await _poll_run(
            "/agents/issue-auditor/status/{run_id}",
            initial,
        )

    endpoint_map = {
        "scan_directory": "/scan",
        "find_in_code": "/find",
        "summarize_file": "/summarize",
        "dependency_analysis": "/dependencies",
        "vector_search": "/vector-search",
    }
    endpoint = endpoint_map.get(name)
    if endpoint is None:
        raise AdapterError(f"unknown tool: {name}")
    return await _request_json("POST", endpoint, payload=arguments)


async def handle(
    message: dict[str, Any],
    tools_cache: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Dispatch one MCP JSON-RPC message."""
    del tools_cache  # Kept as an optional compatibility argument for older imports.
    method = message.get("method", "")
    id_ = message.get("id")
    params = message.get("params") or {}

    if id_ is None:
        return None

    if method == "initialize":
        return _ok(
            id_,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Context Engine is a junior engineer. Prefer "
                    "investigate_codebase for repository investigation. Use advanced "
                    "direct tools only for bounded primitive retrieval. The senior "
                    "engineer verifies evidence and owns all repository writes."
                ),
            },
        )

    if method == "ping":
        return _ok(id_, {})

    if method == "tools/list":
        return _ok(id_, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        if not name:
            return _err(id_, -32602, "'name' is required")
        if name not in TOOL_NAMES:
            return _err(id_, -32602, f"unknown tool: {name}")
        if not isinstance(arguments, dict):
            return _err(id_, -32602, "'arguments' must be an object")

        try:
            result = await _call_tool(name, arguments)
        except AdapterError as exc:
            return _ok(
                id_,
                {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            )

        return _ok(
            id_,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, indent=2, sort_keys=True),
                    }
                ],
                "isError": False,
            },
        )

    return _err(id_, -32601, f"method not found: {method!r}")


async def main() -> None:
    loop = asyncio.get_event_loop()
    stdin_reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(stdin_reader),
        sys.stdin,
    )

    while True:
        try:
            line = await stdin_reader.readline()
        except Exception:
            break
        if not line:
            break

        raw = line.decode(errors="replace").strip()
        if not raw:
            continue

        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            _write(_err(None, -32700, f"parse error: {exc}"))
            continue

        try:
            response = await handle(message)
        except Exception as exc:
            response = _err(message.get("id"), -32603, f"internal error: {exc}")

        if response is not None:
            _write(response)


if __name__ == "__main__":
    asyncio.run(main())
