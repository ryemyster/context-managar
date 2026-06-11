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
    "Context Engine is a read-only junior engineer. Prefer "
    "investigate_codebase for repository investigation instead of manually "
    "chaining advanced retrieval tools. The existing REST agent owns planning, "
    "repository exploration, memory, verification, repair passes, and evidence. "
    "The senior engineer owns architecture decisions and all repository writes."
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
) -> dict[str, Any]:
    return await _delegate(
        "investigate_codebase",
        {
            "task": task,
            "tools": tools or [],
            "max_iterations": max_iterations,
            "system_prompt": system_prompt,
            "allowed_scopes": allowed_scopes,
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
) -> dict[str, Any]:
    return await _delegate(
        "load_context",
        {
            "task": task,
            "paths": paths or [],
            "focus": focus or [],
            "use_vector": use_vector,
        },
    )


@mcp.tool(
    name="review_diff",
    description=(
        "Review a raw git diff through the existing /diff-summary workflow. "
        "Returns summary, risks, touched files, and test recommendations."
    ),
)
async def review_diff(diff: str) -> dict[str, Any]:
    return await _delegate("review_diff", {"diff": diff})


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
        },
    )


@mcp.tool(
    name="scan_directory",
    description=(
        "ADVANCED DIRECT TOOL. Inventory one scoped directory through /scan. "
        "Prefer investigate_codebase when multiple retrieval steps are needed."
    ),
)
async def scan_directory(path: str) -> dict[str, Any]:
    return await _delegate("scan_directory", {"path": path})


@mcp.tool(
    name="find_in_code",
    description=(
        "ADVANCED DIRECT TOOL. Locate a concept through /find. Prefer "
        "investigate_codebase when results require interpretation or verification."
    ),
)
async def find_in_code(query: str, path: str = ".") -> dict[str, Any]:
    return await _delegate("find_in_code", {"query": query, "path": path})


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
async def dependency_analysis(path: str) -> dict[str, Any]:
    return await _delegate("dependency_analysis", {"path": path})


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
) -> dict[str, Any]:
    return await _delegate(
        "vector_search",
        {"query": query, "limit": limit, "threshold": threshold},
    )


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
