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
from pathlib import Path
from typing import Any

import httpx

os.environ.setdefault(
    "OUTPUT_DIR",
    str(Path.home() / "Library/Application Support/context-store/artifacts"),
)
sys.path.insert(0, os.path.dirname(__file__))
from app import artifact_store, tool_registry


ENGINE_BASE = os.getenv("CONTEXT_ENGINE_URL", "http://localhost:8088").rstrip("/")
ENGINE_API_KEY = os.getenv("CONTEXT_ENGINE_API_KEY", "")
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "context-engine"
SERVER_VERSION = "2.0.0"

REQUEST_TIMEOUT = float(os.getenv("CONTEXT_ENGINE_MCP_REQUEST_TIMEOUT", "120"))
RUN_TIMEOUT = float(os.getenv("CONTEXT_ENGINE_MCP_RUN_TIMEOUT", "900"))
POLL_INTERVAL = float(os.getenv("CONTEXT_ENGINE_MCP_POLL_INTERVAL", "1"))
MCP_INLINE_LIMIT = int(os.getenv("MCP_INLINE_LIMIT", "1000"))
MCP_RESPONSE_MODES = ["auto", "summary", "inline", "context_safe"]
DISCOVERY_DETAIL_SCHEMA = {
    "type": "string",
    "enum": ["summary", "standard", "full"],
    "default": "summary",
    "description": "REST discovery detail. summary returns references; full preserves legacy inline payload.",
}
DISCOVERY_LIMIT_SCHEMA = {
    "type": "integer",
    "minimum": 1,
    "maximum": 100,
    "description": "Maximum discovery references returned by REST.",
}
CAPABILITY_SCOPES = {"repo:read", "memory:read", "engine:read"}

CONTROL_PLANE_DESCRIPTION = (
    "Large outputs are written to artifacts. This MCP tool returns references "
    "and concise summaries by default; read artifacts selectively when additional "
    "detail is required. Pass mode='inline' only when the full payload should be "
    "injected into the active conversation."
)

LARGE_RESULT_TOOLS = {
    "investigate_codebase",
    "load_context",
    "review_diff",
    "audit_issue",
    "dependency_analysis",
    "draft_file",
    "scaffold_files",
    "route_analysis",
}


def _schema(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
    *,
    read_only: bool = True,
    destructive: bool = False,
    idempotent: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"{description} {CONTROL_PLANE_DESCRIPTION}",
        "inputSchema": {
            "type": "object",
            "properties": {
                **properties,
                "mode": {
                    "type": "string",
                    "enum": MCP_RESPONSE_MODES,
                    "default": "auto",
                    "description": (
                        "MCP response mode. 'auto' returns a reference when the "
                        "serialized payload exceeds MCP_INLINE_LIMIT. 'summary' "
                        "always returns artifact metadata and a concise summary. "
                        "'inline' preserves the original full response. "
                        "'context_safe' forwards REST mode='context_safe' "
                        "and returns an artifact/reference summary when useful."
                    ),
                },
            },
            "required": required,
            "additionalProperties": False,
        },
        "annotations": {
            "title": name.replace("_", " ").title(),
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
            "idempotentHint": idempotent,
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
                "description": (
                    "Optional internal-agent tool allowlist. Empty enables all. Valid "
                    "names: scan_directory, find_in_code, read_file, grep, "
                    "health_check, search_memory, update_plan. These are internal "
                    "ReAct-loop tools, not MCP tool names — do not pass names like "
                    "investigate_codebase, summarize_file, or vector_search here. "
                    "Unrecognized entries are dropped with a warning rather than "
                    "failing the run."
                ),
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
            "detail": DISCOVERY_DETAIL_SCHEMA,
            "max_results": DISCOVERY_LIMIT_SCHEMA,
            "max_chars": {
                "type": "integer",
                "minimum": 200,
                "description": "Approximate REST response byte budget.",
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
            "detail": DISCOVERY_DETAIL_SCHEMA,
            "max_results": DISCOVERY_LIMIT_SCHEMA,
            "max_chars": {
                "type": "integer",
                "minimum": 200,
                "description": "Approximate REST response byte budget.",
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
            "detail": DISCOVERY_DETAIL_SCHEMA,
            "max_results": DISCOVERY_LIMIT_SCHEMA,
            "max_chars": {
                "type": "integer",
                "minimum": 200,
                "description": "Approximate REST response byte budget.",
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
            "detail": DISCOVERY_DETAIL_SCHEMA,
            "max_results": DISCOVERY_LIMIT_SCHEMA,
            "max_chars": {
                "type": "integer",
                "minimum": 200,
                "description": "Approximate REST response byte budget.",
            },
        },
        ["query"],
    ),
    _schema(
        "store_context_note",
        (
            "CURATED MEMORY WRITE TOOL. Persist an explicit, reviewable note via "
            "POST /store-context-note. Use for plans, architecture decisions, "
            "issue triage notes, and session carry-forward that should become "
            "durable memory. Do not use it for raw transcript dumping or "
            "automatic conversation logging. Retrieval remains separate."
        ),
        {
            "title": {"type": "string", "minLength": 1},
            "content": {"type": "string", "minLength": 1},
            "source": {"type": "string", "minLength": 1},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
            "repo": {
                "type": "string",
                "minLength": 1,
                "description": "Repository identifier, for example ryemyster/ShaleYeah.",
            },
            "scope": {
                "type": "string",
                "enum": ["repo", "project", "session", "global"],
                "description": "Durability scope for retrieval and caller intent.",
            },
        },
        ["title", "content", "source", "repo", "scope"],
        read_only=False,
    ),
    _schema(
        "route_analysis",
        (
            "ADVANCED DIRECT TOOL. Call the existing POST /routes workflow for a "
            "Next.js route inventory. Prefer investigate_codebase when routes "
            "must be interpreted with surrounding code evidence."
        ),
        {
            "path": {
                "type": "string",
                "default": ".",
                "description": "Optional owner/repo path to scope route extraction.",
            },
            "detail": DISCOVERY_DETAIL_SCHEMA,
            "max_results": DISCOVERY_LIMIT_SCHEMA,
            "max_chars": {
                "type": "integer",
                "minimum": 200,
                "description": "Approximate REST response byte budget.",
            },
        },
        [],
    ),
    _schema(
        "draft_file",
        (
            "ADVANCED DIRECT TOOL. Call the existing POST /draft workflow for a "
            "single-file mechanical generation draft. The caller owns review and "
            "all repository writes."
        ),
        {
            "task": {"type": "string", "minLength": 1},
            "file": {"type": "string", "minLength": 1},
            "context_files": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
            "draft_mode": {
                "type": "string",
                "enum": ["create", "edit"],
                "default": "edit",
                "description": "Generation mode passed through to REST /draft.",
            },
        },
        ["task", "file"],
    ),
    _schema(
        "scaffold_files",
        (
            "ADVANCED DIRECT TOOL. Call the existing POST /scaffold workflow for "
            "mechanical multi-file generation. The caller owns review and all "
            "repository writes."
        ),
        {
            "task": {"type": "string", "minLength": 1},
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "minLength": 1},
                        "spec": {"type": "string", "minLength": 1},
                        "mode": {
                            "type": "string",
                            "enum": ["create", "edit"],
                            "default": "create",
                        },
                    },
                    "required": ["file", "spec"],
                    "additionalProperties": False,
                },
                "minItems": 1,
            },
            "context_files": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
        },
        ["task", "files"],
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


def _json_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, sort_keys=True, default=str))


def _token_estimate(payload: dict[str, Any]) -> int:
    return max(1, _json_size(payload) // 4)


def _first_text(value: Any, *, limit: int = 220) -> str:
    if isinstance(value, str) and value.strip():
        text = " ".join(value.split())
        return text[:limit]
    if isinstance(value, list):
        for item in value:
            text = _first_text(item, limit=limit)
            if text:
                return text
    if isinstance(value, dict):
        for key in ("summary", "final_answer", "purpose", "recommendation", "status"):
            text = _first_text(value.get(key), limit=limit)
            if text:
                return text
    return ""


def _summarize_payload(name: str, payload: dict[str, Any]) -> str:
    if name == "investigate_codebase":
        calls = len(payload.get("tool_calls_made") or [])
        stopped = payload.get("stopped_reason") or ""
        status = payload.get("status") or stopped or "complete"
        answer = _first_text(payload.get("final_answer"), limit=180)
        if stopped and stopped != "final_answer":
            partial = answer and not answer.startswith("[agent completed")
            if partial:
                return (
                    f"Agent run partial ({stopped}); {calls} tool calls. "
                    f"{answer}"
                ).strip()
            return (
                f"Agent run inconclusive ({stopped}); {calls} tool calls. "
                f"Diagnostic: {answer}"
            ).strip()
        verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else {}
        if verification and verification.get("passed") is not True:
            return (
                f"Agent run unverified; {calls} tool calls. {answer}"
            ).strip()
        return f"Agent run {status}; {calls} tool calls. {answer}".strip()
    if name == "load_context":
        files = len(payload.get("files") or [])
        risks = len(payload.get("risks") or [])
        summary = _first_text(payload.get("summary"), limit=180)
        return f"Context bundle found {files} files and {risks} risks. {summary}".strip()
    if name == "review_diff":
        files = len(payload.get("files_touched") or [])
        risks = len(payload.get("risks") or [])
        summary = _first_text(payload.get("summary"), limit=180)
        return f"Diff review touched {files} files and found {risks} risks. {summary}".strip()
    if name == "audit_issue":
        findings = len(payload.get("findings") or [])
        status = payload.get("status", "complete")
        return f"Issue audit {status}; {findings} findings."
    if name == "dependency_analysis":
        if "metadata" in payload:
            count = payload.get("metadata", {}).get("result_count", len(payload.get("results") or []))
            return f"Dependency analysis returned {count} reference results."
        internal = len(payload.get("internal") or [])
        external = len(payload.get("external") or [])
        graph = len(payload.get("graph") or {})
        return f"Dependency analysis found {internal} internal imports, {external} external imports, and {graph} graph entries."
    if name == "route_analysis":
        if "metadata" in payload:
            count = payload.get("metadata", {}).get("result_count", len(payload.get("results") or []))
            return f"Route analysis returned {count} reference results."
        routes = len(payload.get("routes") or [])
        api = len(payload.get("api_routes") or [])
        middleware = len(payload.get("middleware") or [])
        return f"Route analysis found {routes} routes, {api} API routes, and {middleware} middleware files."
    if name == "store_context_note":
        return (
            f"Stored context note '{payload.get('artifact_id', 'unknown')}' "
            f"for {payload.get('retrieval_hints', {}).get('filters', {}).get('repo', 'unknown repo')}."
        )
    if name == "draft_file":
        code_len = len(payload.get("code") or "")
        return f"Draft generated for {payload.get('file', 'file')} in {payload.get('mode', 'edit')} mode; {code_len} characters of code."
    if name == "scaffold_files":
        total = payload.get("total", len(payload.get("files") or []))
        errors = len(payload.get("errors") or [])
        return f"Scaffold generated {total} files with {errors} errors."
    return _first_text(payload) or f"{name} completed."


def _artifact_type(name: str) -> str:
    return {
        "investigate_codebase": "agent_run",
        "load_context": "context_bundle",
        "review_diff": "diff_summary",
        "audit_issue": "issue_audit",
        "dependency_analysis": "dependency_graph",
        "store_context_note": "context_note",
        "route_analysis": "routes",
        "draft_file": "draft",
        "scaffold_files": "scaffold",
    }.get(name, name)


def _existing_artifact_path(payload: dict[str, Any]) -> str | None:
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
    for key in ("markdown", "record"):
        if artifacts.get(key):
            return artifacts[key]
    if payload.get("written_to"):
        return payload["written_to"]
    files = payload.get("files")
    if isinstance(files, list):
        for item in files:
            if isinstance(item, dict) and item.get("written_to"):
                return item["written_to"]
    return None


def _reference_response(
    name: str,
    request_arguments: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
    record = artifact_store.write_record(
        tool=f"mcp/{name}",
        request=request_arguments,
        response=payload,
        markdown=None,
    )
    source_artifact_path = _existing_artifact_path(payload)
    artifact_path = source_artifact_path or record["record"]
    return {
        "artifact_id": artifacts.get("event_id") or record["event_id"],
        "artifact_path": artifact_path,
        "artifact_type": _artifact_type(name),
        "summary": _summarize_payload(name, payload),
        "token_estimate": _token_estimate(payload),
        "metadata": {
            "mode": "summary",
            "mcp_record": record["record"],
            "markdown": artifacts.get("markdown") or payload.get("written_to"),
            "source_artifact_path": source_artifact_path,
            "inline_limit": MCP_INLINE_LIMIT,
            "serialized_bytes": _json_size(payload),
        },
    }


def _classify_response(
    name: str,
    arguments: dict[str, Any],
    payload: dict[str, Any],
    response_mode: str,
) -> dict[str, Any]:
    if response_mode == "inline":
        return payload
    if response_mode == "summary":
        return _reference_response(name, arguments, payload)
    if name in LARGE_RESULT_TOOLS and _json_size(payload) > MCP_INLINE_LIMIT:
        return _reference_response(name, arguments, payload)
    return payload


def _pop_response_mode(arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
    rest_arguments = dict(arguments)
    mode = rest_arguments.pop("mode", "auto")
    if mode == "context_safe":
        rest_arguments["mode"] = "context_safe"
        return rest_arguments, "summary"
    if mode not in MCP_RESPONSE_MODES:
        mode = "auto"
    return rest_arguments, mode


# MCP-facing tool names that callers commonly (but incorrectly) pass to the
# `tools` allowlist, mapped to their nearest internal ReAct-loop equivalent.
# See tool_registry.ALL_TOOLS for the authoritative internal tool names.
_TOOL_NAME_ALIASES = {
    "summarize_file": "read_file",
    "vector_search": "search_memory",
}


def _normalize_investigate_tools(arguments: dict[str, Any]) -> dict[str, Any]:
    """Alias MCP-facing tool names and drop unknown ones before hitting /agents/run."""
    normalized = dict(arguments)
    raw_tools = normalized.get("tools")
    if not isinstance(raw_tools, list):
        return normalized

    aliased: list[str] = []
    dropped: list[str] = []
    for entry in raw_tools:
        if not isinstance(entry, str):
            continue
        name = _TOOL_NAME_ALIASES.get(entry, entry)
        if name in tool_registry.ALL_TOOLS:
            if name not in aliased:
                aliased.append(name)
        else:
            dropped.append(entry)

    normalized["tools"] = aliased or None

    if dropped:
        dropped_note = (
            "\n\nIgnored unrecognized entries in `tools` (no internal-agent "
            f"equivalent): {dropped}. Valid internal tool names: "
            f"{tool_registry.ALL_TOOLS}."
        )
        normalized["task"] = str(normalized.get("task", "")).rstrip() + dropped_note

    return normalized


def _normalize_investigate_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Preserve compatibility with callers that send repo paths as allowed_scopes."""
    normalized = _normalize_investigate_tools(arguments)
    raw_scopes = normalized.get("allowed_scopes")
    if not isinstance(raw_scopes, list):
        return normalized

    valid_scopes = [scope for scope in raw_scopes if scope in CAPABILITY_SCOPES]
    path_scopes = [
        scope
        for scope in raw_scopes
        if isinstance(scope, str)
        and scope not in CAPABILITY_SCOPES
        and "/" in scope
        and not scope.startswith("/")
        and ".." not in scope.split("/")
    ]
    invalid_scopes = [
        scope
        for scope in raw_scopes
        if isinstance(scope, str)
        and scope not in CAPABILITY_SCOPES
        and scope not in path_scopes
    ]

    if path_scopes and "repo:read" not in valid_scopes:
        valid_scopes.append("repo:read")
    normalized["allowed_scopes"] = valid_scopes or None

    if path_scopes:
        path_note = (
            "\n\nCaller path constraints: inspect only these repository paths unless "
            "the task explicitly requires broader discovery: "
            + ", ".join(path_scopes)
        )
        normalized["task"] = str(normalized.get("task", "")).rstrip() + path_note
    if invalid_scopes:
        invalid_note = (
            "\n\nIgnored invalid allowed_scopes entries. Use capability scopes "
            "repo:read, memory:read, and engine:read; repository paths belong in "
            f"the task text. Invalid entries: {invalid_scopes}"
        )
        normalized["task"] = str(normalized.get("task", "")).rstrip() + invalid_note

    return normalized


async def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    rest_arguments, response_mode = _pop_response_mode(arguments)
    if name == "draft_file":
        draft_mode = rest_arguments.pop("draft_mode", "edit")
        rest_arguments["mode"] = draft_mode
    original_arguments = dict(rest_arguments)

    if name == "investigate_codebase":
        rest_arguments = _normalize_investigate_arguments(rest_arguments)
        original_arguments = dict(rest_arguments)
        initial = await _request_json("POST", "/agents/run", payload=rest_arguments)
        result = await _poll_run("/agents/run/status/{run_id}", initial)
        return _classify_response(name, original_arguments, result, response_mode)
    if name == "load_context":
        result = await _request_json("POST", "/context", payload=rest_arguments)
        return _classify_response(name, original_arguments, result, response_mode)
    if name == "review_diff":
        result = await _request_json("POST", "/diff-summary", payload=rest_arguments)
        return _classify_response(name, original_arguments, result, response_mode)
    if name == "audit_issue":
        initial = await _request_json(
            "POST", "/agents/issue-auditor/run", payload=rest_arguments
        )
        result = await _poll_run(
            "/agents/issue-auditor/status/{run_id}",
            initial,
        )
        return _classify_response(name, original_arguments, result, response_mode)

    endpoint_map = {
        "scan_directory": "/scan",
        "find_in_code": "/find",
        "summarize_file": "/summarize",
        "dependency_analysis": "/dependencies",
        "vector_search": "/vector-search",
        "store_context_note": "/store-context-note",
        "route_analysis": "/routes",
        "draft_file": "/draft",
        "scaffold_files": "/scaffold",
    }
    endpoint = endpoint_map.get(name)
    if endpoint is None:
        raise AdapterError(f"unknown tool: {name}")
    result = await _request_json("POST", endpoint, payload=rest_arguments)
    return _classify_response(name, original_arguments, result, response_mode)


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
                    "engineer verifies evidence and owns all repository writes. "
                    "Large outputs are written to artifacts. MCP returns references "
                    "and summaries by default; read artifacts selectively when "
                    "additional detail is required."
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
