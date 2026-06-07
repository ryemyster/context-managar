"""
Tests for mcp_server.py — MCP JSON-RPC 2.0 protocol adapter.

Tests the dispatch logic (handle()) without touching the filesystem or network.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'context-engine'))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# mcp_server is a standalone script — import it directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'context-engine'))
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "mcp_server",
    os.path.join(os.path.dirname(__file__), '..', 'context-engine', 'mcp_server.py'),
)
mcp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mcp)


# ── handle() dispatch ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_initialize_returns_protocol_version():
    msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    response = await mcp.handle(msg, [])
    assert response["id"] == 1
    result = response["result"]
    assert result["protocolVersion"] == mcp.PROTOCOL_VERSION
    assert "capabilities" in result
    assert "serverInfo" in result


@pytest.mark.asyncio
async def test_ping_returns_empty_result():
    msg = {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}
    response = await mcp.handle(msg, [])
    assert response["id"] == 2
    assert response["result"] == {}


@pytest.mark.asyncio
async def test_tools_list_refreshes_cache_and_returns_tools():
    fake_tools = [{"name": "scan_directory", "description": "scan", "inputSchema": {}}]

    with patch.object(mcp, "_fetch_tools", new_callable=AsyncMock, return_value=fake_tools):
        tools_cache = []
        msg = {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
        response = await mcp.handle(msg, tools_cache)

    assert response["id"] == 3
    assert response["result"]["tools"] == fake_tools
    assert tools_cache == fake_tools  # cache was updated in place


@pytest.mark.asyncio
async def test_tools_call_success():
    with patch.object(mcp, "_call_tool", new_callable=AsyncMock, return_value="health: ok"):
        msg = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "health_check", "arguments": {}},
        }
        response = await mcp.handle(msg, [])

    assert response["id"] == 4
    result = response["result"]
    assert result["content"][0]["type"] == "text"
    assert result["content"][0]["text"] == "health: ok"
    assert result["isError"] is False


@pytest.mark.asyncio
async def test_tools_call_error_result_sets_is_error():
    with patch.object(mcp, "_call_tool", new_callable=AsyncMock, return_value="[error: engine down]"):
        msg = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "health_check", "arguments": {}},
        }
        response = await mcp.handle(msg, [])

    assert response["result"]["isError"] is True


@pytest.mark.asyncio
async def test_tools_call_missing_name_returns_error():
    msg = {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {}}
    response = await mcp.handle(msg, [])
    assert "error" in response
    assert response["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_unknown_method_returns_method_not_found():
    msg = {"jsonrpc": "2.0", "id": 7, "method": "unknown/method", "params": {}}
    response = await mcp.handle(msg, [])
    assert "error" in response
    assert response["error"]["code"] == -32601
    assert "unknown/method" in response["error"]["message"]


@pytest.mark.asyncio
async def test_notification_returns_none():
    """Messages without 'id' are notifications — no response."""
    msg = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
    response = await mcp.handle(msg, [])
    assert response is None


# ── _fetch_tools ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_tools_converts_to_mcp_format():
    """_fetch_tools converts function-call schema format to MCP inputSchema format."""
    engine_response = {
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "scan_directory",
                    "description": "List files",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            }
        ]
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = engine_response  # json() is sync in httpx

    import httpx
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        tools = await mcp._fetch_tools()

    assert len(tools) == 1
    t = tools[0]
    assert t["name"] == "scan_directory"
    assert t["description"] == "List files"
    assert "inputSchema" in t
    assert "parameters" not in t  # should use inputSchema, not parameters


@pytest.mark.asyncio
async def test_fetch_tools_returns_empty_on_engine_down():
    """Falls back to empty list if the engine is unavailable."""
    import httpx

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ConnectError("refused")
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        tools = await mcp._fetch_tools()

    assert tools == []


# ── _call_tool ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_call_tool_returns_result_string():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"name": "health_check", "result": "health: ok"}

    import httpx
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await mcp._call_tool("health_check", {})

    assert result == "health: ok"


@pytest.mark.asyncio
async def test_call_tool_timeout_returns_error_string():
    import httpx

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.TimeoutException("timed out")
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await mcp._call_tool("slow_tool", {})

    assert "[error:" in result
    assert "timed out" in result or "slow_tool" in result
