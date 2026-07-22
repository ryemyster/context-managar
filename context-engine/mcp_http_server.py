#!/usr/bin/env python3
"""Persistent Streamable HTTP transport for the context-engine MCP adapter."""

from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from mcp_server import AdapterError, _call_tool


MCP_HOST = os.getenv("CONTEXT_ENGINE_MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("CONTEXT_ENGINE_MCP_PORT", "8089"))

INSTRUCTIONS = (
    "Context Engine is a junior engineer. Prefer "
    "investigate_codebase for repository investigation instead of manually "
    "chaining advanced retrieval tools. The existing REST agent owns planning, "
    "repository exploration, memory, verification, repair passes, and evidence. "
    "The senior engineer owns architecture decisions and all repository writes. "
    "Curated note storage is explicit through store_context_note only. "
    "Large outputs are written to artifacts; MCP returns references and summaries "
    "by default. Read artifacts selectively when additional detail is required."
)

mcp = FastMCP(
    name="context-engine",
    instructions=INSTRUCTIONS,
    host=MCP_HOST,
    port=MCP_PORT,
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
)


async def _delegate(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        return await _call_tool(name, arguments)
    except AdapterError as exc:
        raise RuntimeError(str(exc)) from exc


@mcp.tool(
    name="investigate_codebase",
    description=(
        "PRIMARY DELEGATION TOOL. Delegate repository investigation to the "
        "existing Context Engine junior engineer. Prefer this over manually "
        "chaining scan, search, summarize, dependency, or vector tools. The REST "
        "agent plans, searches memory, explores files, verifies conclusions, runs "
        "repair passes, and returns an evidence trail."
    ),
)
async def investigate_codebase(
    task: str,
    tools: list[str] | None = None,
    max_iterations: int = 10,
    system_prompt: str | None = None,
    allowed_scopes: list[str] | None = None,
    mode: str = "auto",
) -> dict[str, Any]:
    return await _delegate(
        "investigate_codebase",
        {
            "task": task,
            "tools": tools or [],
            "max_iterations": max_iterations,
            "system_prompt": system_prompt,
            "allowed_scopes": allowed_scopes,
            "mode": mode,
        },
    )


@mcp.tool(
    name="load_context",
    description=(
        "Gather a bounded pre-task context bundle through the existing /context "
        "workflow. Prefer investigate_codebase for open-ended investigation or "
        "conclusions requiring a verified evidence trail."
    ),
)
async def load_context(
    task: str,
    paths: list[str] | None = None,
    focus: list[str] | None = None,
    use_vector: bool = False,
    mode: str = "auto",
) -> dict[str, Any]:
    return await _delegate(
        "load_context",
        {
            "task": task,
            "paths": paths or [],
            "focus": focus or [],
            "use_vector": use_vector,
            "mode": mode,
        },
    )


@mcp.tool(
    name="review_diff",
    description=(
        "Review a raw git diff through the existing /diff-summary workflow. "
        "Returns summary, risks, touched files, and test recommendations."
    ),
)
async def review_diff(diff: str, mode: str = "auto") -> dict[str, Any]:
    return await _delegate("review_diff", {"diff": diff, "mode": mode})


@mcp.tool(
    name="audit_issue",
    description=(
        "Delegate evidence-based issue investigation to the existing asynchronous "
        "issue auditor and return the completed findings."
    ),
)
async def audit_issue(
    task: str,
    repo: str,
    paths: list[str],
    focus: list[str] | None = None,
    requirements: list[str] | None = None,
    use_vector: bool = False,
    mode: str = "auto",
) -> dict[str, Any]:
    return await _delegate(
        "audit_issue",
        {
            "task": task,
            "repo": repo,
            "paths": paths,
            "focus": focus or [],
            "requirements": requirements or [],
            "use_vector": use_vector,
            "mode": mode,
        },
    )


@mcp.tool(
    name="scan_directory",
    description=(
        "ADVANCED DIRECT TOOL. Inventory one scoped directory through /scan. "
        "Prefer investigate_codebase when multiple retrieval steps are needed."
    ),
)
async def scan_directory(
    path: str,
    detail: str = "summary",
    max_results: int | None = None,
    max_chars: int | None = None,
    mode: str = "auto",
) -> dict[str, Any]:
    arguments: dict[str, Any] = {"path": path, "detail": detail, "mode": mode}
    if max_results is not None:
        arguments["max_results"] = max_results
    if max_chars is not None:
        arguments["max_chars"] = max_chars
    return await _delegate("scan_directory", arguments)


@mcp.tool(
    name="find_in_code",
    description=(
        "ADVANCED DIRECT TOOL. Locate a concept through /find. Prefer "
        "investigate_codebase when results require interpretation or verification."
    ),
)
async def find_in_code(
    query: str,
    path: str = ".",
    detail: str = "summary",
    max_results: int | None = None,
    max_chars: int | None = None,
    mode: str = "auto",
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "query": query,
        "path": path,
        "detail": detail,
        "mode": mode,
    }
    if max_results is not None:
        arguments["max_results"] = max_results
    if max_chars is not None:
        arguments["max_chars"] = max_chars
    return await _delegate("find_in_code", arguments)


@mcp.tool(
    name="summarize_file",
    description=(
        "ADVANCED DIRECT TOOL. Summarize one known file through /summarize. "
        "Prefer investigate_codebase for discovery or cross-file reasoning."
    ),
)
async def summarize_file(file: str) -> dict[str, Any]:
    return await _delegate("summarize_file", {"file": file})


@mcp.tool(
    name="dependency_analysis",
    description=(
        "ADVANCED DIRECT TOOL. Build an import graph through /dependencies. "
        "Prefer investigate_codebase for architectural conclusions."
    ),
)
async def dependency_analysis(
    path: str,
    detail: str = "summary",
    max_results: int | None = None,
    max_chars: int | None = None,
    mode: str = "auto",
) -> dict[str, Any]:
    arguments: dict[str, Any] = {"path": path, "detail": detail, "mode": mode}
    if max_results is not None:
        arguments["max_results"] = max_results
    if max_chars is not None:
        arguments["max_chars"] = max_chars
    return await _delegate("dependency_analysis", arguments)


@mcp.tool(
    name="store_context_note",
    description=(
        "CURATED MEMORY WRITE TOOL. Persist an explicit, reviewable note through "
        "/store-context-note. Use for plans, architecture decisions, issue "
        "triage notes, and session carry-forward that should become durable "
        "memory. Do not use it for raw transcript dumping or automatic "
        "conversation logging."
    ),
)
async def store_context_note(
    title: str,
    content: str,
    source: str,
    repo: str,
    scope: str,
    tags: list[str] | None = None,
    mode: str = "auto",
) -> dict[str, Any]:
    return await _delegate(
        "store_context_note",
        {
            "title": title,
            "content": content,
            "source": source,
            "repo": repo,
            "scope": scope,
            "tags": tags or [],
            "mode": mode,
        },
    )


@mcp.tool(
    name="route_analysis",
    description=(
        "ADVANCED DIRECT TOOL. Extract a route inventory through /routes. Large "
        "outputs are written to artifacts; the tool returns references and "
        "summaries by default."
    ),
)
async def route_analysis(
    path: str = ".",
    detail: str = "summary",
    max_results: int | None = None,
    max_chars: int | None = None,
    mode: str = "auto",
) -> dict[str, Any]:
    arguments: dict[str, Any] = {"path": path, "detail": detail, "mode": mode}
    if max_results is not None:
        arguments["max_results"] = max_results
    if max_chars is not None:
        arguments["max_chars"] = max_chars
    return await _delegate("route_analysis", arguments)


@mcp.tool(
    name="draft_file",
    description=(
        "ADVANCED DIRECT TOOL. Generate a single-file draft through /draft. The "
        "caller owns review and all repository writes. Large outputs are written "
        "to artifacts by default."
    ),
)
async def draft_file(
    task: str,
    file: str,
    context_files: list[str] | None = None,
    draft_mode: str = "edit",
    mode: str = "auto",
) -> dict[str, Any]:
    return await _delegate(
        "draft_file",
        {
            "task": task,
            "file": file,
            "context_files": context_files or [],
            "draft_mode": draft_mode,
            "mode": mode,
        },
    )


@mcp.tool(
    name="scaffold_files",
    description=(
        "ADVANCED DIRECT TOOL. Generate multi-file drafts through /scaffold. The "
        "caller owns review and all repository writes. Large outputs are written "
        "to artifacts by default."
    ),
)
async def scaffold_files(
    task: str,
    files: list[dict[str, Any]],
    context_files: list[str] | None = None,
    mode: str = "auto",
) -> dict[str, Any]:
    return await _delegate(
        "scaffold_files",
        {
            "task": task,
            "files": files,
            "context_files": context_files or [],
            "mode": mode,
        },
    )


@mcp.tool(
    name="vector_search",
    description=(
        "ADVANCED DIRECT TOOL. Retrieve semantically similar indexed chunks "
        "through /vector-search. This is not a replacement for agent delegation."
    ),
)
async def vector_search(
    query: str,
    limit: int = 8,
    threshold: float = 0.3,
    detail: str = "summary",
    max_results: int | None = None,
    max_chars: int | None = None,
    mode: str = "auto",
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "query": query,
        "limit": limit,
        "threshold": threshold,
        "detail": detail,
        "mode": mode,
    }
    if max_results is not None:
        arguments["max_results"] = max_results
    if max_chars is not None:
        arguments["max_chars"] = max_chars
    return await _delegate("vector_search", arguments)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
