"""Tests for the persistent Streamable HTTP MCP entrypoint."""

import importlib.util
import os
import sys

import pytest
from unittest.mock import AsyncMock, patch


pytest.importorskip("mcp")

CONTEXT_ENGINE_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "context-engine",
)
sys.path.insert(0, CONTEXT_ENGINE_DIR)

_spec = importlib.util.spec_from_file_location(
    "mcp_http_server",
    os.path.join(CONTEXT_ENGINE_DIR, "mcp_http_server.py"),
)
mcp_http = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mcp_http)


def test_http_server_configuration():
    assert mcp_http.mcp.name == "context-engine"
    assert mcp_http.mcp.settings.host == "127.0.0.1"
    assert mcp_http.mcp.settings.port == 8089
    assert mcp_http.mcp.settings.streamable_http_path == "/mcp"
    assert mcp_http.mcp.settings.stateless_http is True
    assert mcp_http.mcp.settings.json_response is True
    assert "Prefer investigate_codebase" in mcp_http.INSTRUCTIONS
    assert "Large outputs are written to artifacts" in mcp_http.INSTRUCTIONS


@pytest.mark.asyncio
async def test_investigate_codebase_delegates_to_existing_adapter():
    expected = {"final_answer": "done", "verification": {"passed": True}}
    with patch.object(
        mcp_http,
        "_call_tool",
        new_callable=AsyncMock,
        return_value=expected,
    ) as call:
        result = await mcp_http.investigate_codebase(
            task="Inspect auth",
            tools=["grep"],
            max_iterations=7,
            allowed_scopes=["repo:read"],
        )

    assert result == expected
    call.assert_awaited_once_with(
        "investigate_codebase",
        {
            "task": "Inspect auth",
            "tools": ["grep"],
            "max_iterations": 7,
            "system_prompt": None,
            "allowed_scopes": ["repo:read"],
            "mode": "auto",
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("function_name", "tool_name", "arguments", "kwargs"),
    [
        (
            "load_context",
            "load_context",
            {"task": "Load", "paths": [], "focus": [], "use_vector": False, "mode": "auto"},
            {"task": "Load"},
        ),
        (
            "review_diff",
            "review_diff",
            {"diff": "diff --git a/a b/a", "mode": "auto"},
            {"diff": "diff --git a/a b/a"},
        ),
        (
            "audit_issue",
            "audit_issue",
            {
                "task": "Audit",
                "repo": "owner/repo",
                "paths": ["src"],
                "focus": [],
                "requirements": [],
                "use_vector": False,
                "mode": "auto",
            },
            {"task": "Audit", "repo": "owner/repo", "paths": ["src"]},
        ),
        (
            "scan_directory",
            "scan_directory",
            {"path": "owner/repo/src", "detail": "summary", "mode": "auto"},
            {"path": "owner/repo/src"},
        ),
        (
            "find_in_code",
            "find_in_code",
            {"query": "auth", "path": ".", "detail": "summary", "mode": "auto"},
            {"query": "auth"},
        ),
        (
            "summarize_file",
            "summarize_file",
            {"file": "owner/repo/app.py"},
            {"file": "owner/repo/app.py"},
        ),
        (
            "dependency_analysis",
            "dependency_analysis",
            {"path": "owner/repo/src", "detail": "summary", "mode": "auto"},
            {"path": "owner/repo/src"},
        ),
        (
            "route_analysis",
            "route_analysis",
            {"path": ".", "detail": "summary", "mode": "auto"},
            {},
        ),
        (
            "draft_file",
            "draft_file",
            {
                "task": "Draft",
                "file": "owner/repo/a.py",
                "context_files": [],
                "draft_mode": "edit",
                "mode": "auto",
            },
            {"task": "Draft", "file": "owner/repo/a.py"},
        ),
        (
            "scaffold_files",
            "scaffold_files",
            {
                "task": "Scaffold",
                "files": [{"file": "owner/repo/a.py", "spec": "Create it"}],
                "context_files": [],
                "mode": "auto",
            },
            {"task": "Scaffold", "files": [{"file": "owner/repo/a.py", "spec": "Create it"}]},
        ),
        (
            "vector_search",
            "vector_search",
            {
                "query": "auth",
                "limit": 8,
                "threshold": 0.3,
                "detail": "summary",
                "mode": "auto",
            },
            {"query": "auth"},
        ),
    ],
)
async def test_tools_are_thin_adapter_calls(
    function_name,
    tool_name,
    arguments,
    kwargs,
):
    with patch.object(
        mcp_http,
        "_call_tool",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ) as call:
        result = await getattr(mcp_http, function_name)(**kwargs)

    assert result == {"ok": True}
    call.assert_awaited_once_with(tool_name, arguments)


@pytest.mark.asyncio
async def test_http_advanced_tools_forward_context_safe_options():
    with patch.object(
        mcp_http,
        "_call_tool",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ) as call:
        result = await mcp_http.find_in_code(
            query="auth",
            path="owner/repo/src",
            mode="context_safe",
            max_results=5,
            max_chars=1200,
        )

    assert result == {"ok": True}
    call.assert_awaited_once_with(
        "find_in_code",
        {
            "query": "auth",
            "path": "owner/repo/src",
            "detail": "summary",
            "mode": "context_safe",
            "max_results": 5,
            "max_chars": 1200,
        },
    )


@pytest.mark.asyncio
async def test_adapter_errors_become_tool_failures():
    with patch.object(
        mcp_http,
        "_call_tool",
        new_callable=AsyncMock,
        side_effect=mcp_http.AdapterError("REST unavailable"),
    ):
        with pytest.raises(RuntimeError, match="REST unavailable"):
            await mcp_http.scan_directory("owner/repo/src")
