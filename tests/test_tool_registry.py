"""
Tests for tool_registry.py — tool definitions and executors.

Mocks safe_resolve, walk_repo, read_file, find_in_repo, and grep_pattern
so no filesystem or Ollama calls are made.
"""

import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'context-engine'))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException
from pathlib import Path

from app import tool_registry
from app.tool_registry import (
    ALL_TOOLS,
    ToolResult,
    execute_tool,
    get_tool_definitions,
    get_tool_metadata,
    SCOPE_REPO_READ,
    SCOPE_MEMORY_READ,
    SCOPE_ENGINE_READ,
)


# ── get_tool_definitions ────────────────────────────────────────────────────────

class TestGetToolDefinitions:
    def test_all_tools_returned_when_names_is_none(self):
        defs = get_tool_definitions(None)
        names = [d["function"]["name"] for d in defs]
        assert set(names) == set(ALL_TOOLS)

    def test_subset_returned_when_names_provided(self):
        defs = get_tool_definitions(["scan_directory", "read_file"])
        names = [d["function"]["name"] for d in defs]
        assert names == ["scan_directory", "read_file"]

    def test_unknown_names_silently_skipped(self):
        defs = get_tool_definitions(["scan_directory", "nonexistent_tool"])
        names = [d["function"]["name"] for d in defs]
        assert names == ["scan_directory"]

    def test_each_definition_has_required_fields(self):
        for d in get_tool_definitions():
            assert d["type"] == "function"
            fn = d["function"]
            assert "name" in fn
            assert "description" in fn
            assert "parameters" in fn

    def test_empty_list_returns_all_tools(self):
        assert get_tool_definitions([]) == get_tool_definitions(None)


# ── get_tool_metadata ──────────────────────────────────────────────────────────

def test_get_tool_metadata_known_tool():
    meta = get_tool_metadata("read_file")
    assert meta is not None
    assert SCOPE_REPO_READ in meta.scopes

def test_get_tool_metadata_unknown_returns_none():
    assert get_tool_metadata("nonexistent_tool") is None

def test_get_tool_metadata_update_plan_has_empty_scopes():
    meta = get_tool_metadata("update_plan")
    assert meta is not None
    assert meta.scopes == []


# ── execute_tool — unknown tool ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_tool_unknown_returns_error():
    result = await execute_tool("nonexistent_tool", {})
    assert result.ok is False
    assert "unknown tool" in result.data
    assert "nonexistent_tool" in result.data
    assert result.error_type == "invalid_input"
    assert result.recovery_hint and "Available" in result.recovery_hint


# ── scope enforcement ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scope_denied_when_tool_scope_not_in_allowed():
    result = await execute_tool("read_file", {"file": "owner/repo/f.py"},
                                 allowed_scopes=["memory:read"])
    assert result.ok is False
    assert result.error_type == "scope_denied"
    assert "read_file" in result.data
    assert result.retryable is False

@pytest.mark.asyncio
async def test_scope_allowed_when_matching_scope_present():
    content = "line1\nline2\n"
    with patch("app.tool_registry.safe_resolve", return_value=Path("/repos/owner/repo/f.py")), \
         patch("app.tool_registry._read_file", return_value=content):
        result = await execute_tool("read_file", {"file": "owner/repo/f.py"},
                                     allowed_scopes=["repo:read"])
    assert result.ok is True

@pytest.mark.asyncio
async def test_update_plan_bypasses_scope_enforcement():
    """update_plan has empty scopes so it always runs regardless of allowed_scopes."""
    result = await execute_tool("update_plan",
                                 {"goal": "test goal", "steps": ["step one"]},
                                 allowed_scopes=["repo:read"])
    assert result.ok is True

@pytest.mark.asyncio
async def test_all_scopes_permitted_when_allowed_scopes_is_none():
    """allowed_scopes=None means no restriction."""
    content = "hello"
    with patch("app.tool_registry.safe_resolve", return_value=Path("/repos/owner/repo/f.py")), \
         patch("app.tool_registry._read_file", return_value=content):
        result = await execute_tool("read_file", {"file": "owner/repo/f.py"},
                                     allowed_scopes=None)
    assert result.ok is True


# ── scan_directory executor ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scan_directory_missing_path():
    result = await execute_tool("scan_directory", {})
    assert result.ok is False
    assert result.error_type == "invalid_input"
    assert "path" in result.data.lower()


@pytest.mark.asyncio
async def test_scan_directory_rejected_path():
    with patch("app.tool_registry.safe_resolve", side_effect=HTTPException(400, "path too broad")):
        result = await execute_tool("scan_directory", {"path": "."})
    assert result.ok is False
    assert result.error_type == "path_rejected"
    assert result.recovery_hint and "owner/repo/subdir" in result.recovery_hint


@pytest.mark.asyncio
async def test_scan_directory_returns_file_list():
    fake_files = [Path("/repos/owner/repo/app/main.py"), Path("/repos/owner/repo/app/config.py")]

    with patch("app.tool_registry.safe_resolve", return_value=Path("/repos/owner/repo/app")), \
         patch("app.tool_registry.walk_repo", return_value=fake_files), \
         patch("app.tool_registry.rel_path", side_effect=lambda p: str(p).replace("/repos/", "")):
        result = await execute_tool("scan_directory", {"path": "owner/repo/app"})

    assert result.ok is True
    data = json.loads(result.data)
    assert data["count"] == 2
    assert "owner/repo/app/main.py" in data["files"]


@pytest.mark.asyncio
async def test_scan_directory_truncates_large_result():
    many_files = [Path(f"/repos/owner/repo/f{i}.py") for i in range(500)]

    with patch("app.tool_registry.safe_resolve", return_value=Path("/repos/owner/repo")), \
         patch("app.tool_registry.walk_repo", return_value=many_files), \
         patch("app.tool_registry.rel_path", side_effect=lambda p: str(p).replace("/repos/", "")):
        result = await execute_tool("scan_directory", {"path": "owner/repo"})

    assert result.ok is True
    assert "truncated" in result.data


# ── find_in_code executor ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_find_in_code_missing_query():
    result = await execute_tool("find_in_code", {})
    assert result.ok is False
    assert result.error_type == "invalid_input"
    assert "query" in result.data.lower()


@pytest.mark.asyncio
async def test_find_in_code_no_matches():
    with patch("app.tool_registry.find_in_repo", return_value=[]):
        result = await execute_tool("find_in_code", {"query": "something rare"})
    assert result.ok is True
    assert "no matches" in result.data


@pytest.mark.asyncio
async def test_find_in_code_returns_formatted_matches():
    matches = [
        {"path": "owner/repo/app/main.py", "line_no": 42, "line": "def run_agent(task):"},
        {"path": "owner/repo/app/agent_runner.py", "line_no": 10, "line": "async def run_agent("},
    ]
    with patch("app.tool_registry.find_in_repo", return_value=matches):
        result = await execute_tool("find_in_code", {"query": "run_agent"})

    assert result.ok is True
    assert "owner/repo/app/main.py:42:" in result.data
    assert "def run_agent(task):" in result.data


@pytest.mark.asyncio
async def test_find_in_code_rejected_path():
    with patch("app.tool_registry.find_in_repo", side_effect=HTTPException(400, "path bad")):
        result = await execute_tool("find_in_code", {"query": "foo", "path": "."})
    assert result.ok is False
    assert result.error_type == "path_rejected"


# ── read_file executor ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_read_file_missing_file_arg():
    result = await execute_tool("read_file", {})
    assert result.ok is False
    assert result.error_type == "invalid_input"
    assert "file" in result.data.lower()


@pytest.mark.asyncio
async def test_read_file_rejected_path():
    with patch("app.tool_registry.safe_resolve", side_effect=HTTPException(400, "path traversal")):
        result = await execute_tool("read_file", {"file": "../../etc/passwd"})
    assert result.ok is False
    assert result.error_type == "path_rejected"
    assert result.recovery_hint and "owner/repo/path/to/file.py" in result.recovery_hint


@pytest.mark.asyncio
async def test_read_file_not_found():
    with patch("app.tool_registry.safe_resolve", return_value=Path("/repos/owner/repo/missing.py")), \
         patch("app.tool_registry._read_file", return_value=None):
        result = await execute_tool("read_file", {"file": "owner/repo/missing.py"})
    assert result.ok is False
    assert result.error_type == "not_found"


@pytest.mark.asyncio
async def test_read_file_returns_content():
    content = "line1\nline2\nline3\n"
    with patch("app.tool_registry.safe_resolve", return_value=Path("/repos/owner/repo/app.py")), \
         patch("app.tool_registry._read_file", return_value=content):
        result = await execute_tool("read_file", {"file": "owner/repo/app.py"})
    assert result.ok is True
    assert "line1" in result.data
    assert "line2" in result.data


@pytest.mark.asyncio
async def test_read_file_offset_and_limit():
    content = "\n".join(f"line{i}" for i in range(1, 21))
    with patch("app.tool_registry.safe_resolve", return_value=Path("/repos/owner/repo/app.py")), \
         patch("app.tool_registry._read_file", return_value=content):
        result = await execute_tool("read_file", {"file": "owner/repo/app.py", "offset": 5, "limit": 3})
    assert result.ok is True
    lines = result.data.splitlines()
    assert lines[0] == "line5"
    assert len(lines) == 3


# ── grep executor ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_grep_missing_pattern():
    result = await execute_tool("grep", {})
    assert result.ok is False
    assert result.error_type == "invalid_input"
    assert "pattern" in result.data.lower()


@pytest.mark.asyncio
async def test_grep_no_matches():
    with patch("app.tool_registry.safe_resolve", return_value=Path("/repos/owner/repo")), \
         patch("app.tool_registry.walk_repo", return_value=[Path("/repos/owner/repo/app.py")]), \
         patch("app.tool_registry._read_file", return_value="hello world"), \
         patch("app.tool_registry.grep_pattern", return_value=[]):
        result = await execute_tool("grep", {"pattern": "zzznomatch", "path": "owner/repo"})
    assert result.ok is True
    assert "no matches" in result.data


@pytest.mark.asyncio
async def test_grep_returns_formatted_hits():
    with patch("app.tool_registry.safe_resolve", return_value=Path("/repos/owner/repo")), \
         patch("app.tool_registry.walk_repo", return_value=[Path("/repos/owner/repo/main.py")]), \
         patch("app.tool_registry._read_file", return_value="async def run_agent():"), \
         patch("app.tool_registry.grep_pattern", return_value=["async def run_agent():"]), \
         patch("app.tool_registry.rel_path", return_value="owner/repo/main.py"):
        result = await execute_tool("grep", {"pattern": "run_agent"})
    assert result.ok is True
    assert "owner/repo/main.py:" in result.data
    assert "async def run_agent" in result.data


@pytest.mark.asyncio
async def test_grep_rejected_path():
    with patch("app.tool_registry.safe_resolve", side_effect=HTTPException(400, "bad path")):
        result = await execute_tool("grep", {"pattern": "foo", "path": "owner/repo/bad"})
    assert result.ok is False
    assert result.error_type == "path_rejected"


# ── health_check executor ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_check_ok():
    mock_response = MagicMock()
    mock_response.status_code = 200

    with patch("app.tool_registry._httpx", create=True), \
         patch("httpx.get", return_value=mock_response):
        result = await execute_tool("health_check", {})
    assert result.ok is True
    assert "ok" in result.data or "HTTP 200" in result.data or "health" in result.data


@pytest.mark.asyncio
async def test_health_check_connection_error():
    import httpx
    with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
        result = await execute_tool("health_check", {})
    assert result.ok is False
    assert result.error_type == "engine_down"
    assert "health_check failed" in result.data


# ── search_memory executor ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_memory_missing_query():
    result = await execute_tool("search_memory", {})
    assert result.ok is False
    assert result.error_type == "invalid_input"
    assert "query" in result.data.lower()


@pytest.mark.asyncio
async def test_search_memory_skips_embed_when_vector_unavailable():
    with patch("app.tool_registry.supabase_vector.is_available", new_callable=AsyncMock,
               return_value=False), \
         patch("app.tool_registry.ollama_client.embed", new_callable=AsyncMock) as mock_embed:
        result = await execute_tool("search_memory", {"query": "route handlers"})
    assert result.ok is True
    assert "[no memory found" in result.data
    mock_embed.assert_not_called()


@pytest.mark.asyncio
async def test_search_memory_embed_failure():
    with patch("app.tool_registry.supabase_vector.is_available", new_callable=AsyncMock,
               return_value=True), \
         patch("app.tool_registry.ollama_client.embed", new_callable=AsyncMock,
               side_effect=Exception("Ollama down")):
        result = await execute_tool("search_memory", {"query": "route handlers"})
    assert result.ok is False
    assert result.error_type == "engine_down"
    assert "Ollama down" in result.data


@pytest.mark.asyncio
async def test_search_memory_no_results():
    with patch("app.tool_registry.supabase_vector.is_available", new_callable=AsyncMock,
               return_value=True), \
         patch("app.tool_registry.ollama_client.embed", new_callable=AsyncMock,
               return_value=[0.1] * 768), \
         patch("app.tool_registry.supabase_vector.search", new_callable=AsyncMock,
               return_value=[]):
        result = await execute_tool("search_memory", {"query": "something never seen before"})
    assert result.ok is True
    assert "[no memory found" in result.data


@pytest.mark.asyncio
async def test_search_memory_returns_formatted_results():
    fake_results = [
        {"path": "artifact://agents/run/abc123", "chunk": "route handlers: /scan /find /context", "similarity": 0.87},
        {"path": "ryemyster/context-manager/context-engine/app/main.py", "chunk": "@app.post('/scan')", "similarity": 0.72},
    ]
    with patch("app.tool_registry.supabase_vector.is_available", new_callable=AsyncMock,
               return_value=True), \
         patch("app.tool_registry.ollama_client.embed", new_callable=AsyncMock,
               return_value=[0.1] * 768), \
         patch("app.tool_registry.supabase_vector.search", new_callable=AsyncMock,
               return_value=fake_results):
        result = await execute_tool("search_memory", {"query": "route handlers"})

    assert result.ok is True
    assert "[0.87]" in result.data
    assert "artifact://agents/run/abc123" in result.data
    assert "route handlers: /scan /find /context" in result.data
    assert "---" in result.data


@pytest.mark.asyncio
async def test_search_memory_limit_capped_at_10():
    captured = {}

    async def fake_search(embedding, limit, threshold):
        captured["limit"] = limit
        return []

    with patch("app.tool_registry.supabase_vector.is_available", new_callable=AsyncMock,
               return_value=True), \
         patch("app.tool_registry.ollama_client.embed", new_callable=AsyncMock,
               return_value=[0.1] * 768), \
         patch("app.tool_registry.supabase_vector.search", side_effect=fake_search):
        await execute_tool("search_memory", {"query": "test", "limit": 99})

    assert captured["limit"] == 10


# ── update_plan executor ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_plan_missing_goal():
    result = await execute_tool("update_plan", {"steps": ["step one"]})
    assert result.ok is False
    assert result.error_type == "invalid_input"
    assert "goal" in result.data


@pytest.mark.asyncio
async def test_update_plan_missing_steps():
    result = await execute_tool("update_plan", {"goal": "Find all routes"})
    assert result.ok is False
    assert result.error_type == "invalid_input"
    assert "steps" in result.data


@pytest.mark.asyncio
async def test_update_plan_valid_returns_json():
    result = await execute_tool("update_plan", {
        "goal": "Find all route handlers",
        "steps": ["search_memory", "scan_directory", "read_file"],
        "current_step": 1,
        "blockers": ["might be large file"],
    })
    assert result.ok is True
    plan = json.loads(result.data)
    assert plan["goal"] == "Find all route handlers"
    assert plan["steps"] == ["search_memory", "scan_directory", "read_file"]
    assert plan["current_step"] == 1
    assert plan["blockers"] == ["might be large file"]


@pytest.mark.asyncio
async def test_update_plan_defaults_current_step_and_blockers():
    result = await execute_tool("update_plan", {
        "goal": "Simple goal",
        "steps": ["step one"],
    })
    assert result.ok is True
    plan = json.loads(result.data)
    assert plan["current_step"] == 0
    assert plan["blockers"] == []
